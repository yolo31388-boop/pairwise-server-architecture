"""
服务器架构与负载均衡系统 - 核心模块（修复版）

修复内容：
- 区域路由：综合延迟与负载选择区域，首选区域满载时回退到次近区域
- 负载均衡：综合在线人数/CPU/内存/带宽多维指标，支持服务器权重
- 会话迁移：完整迁移会话数据，支持超时与一致性校验
- 故障转移：健康检查 + 崩溃服务器会话迁移 + 全量崩溃时的降级方案
- 跨区对战：延迟补偿 + 跨区域数据同步 + 区域限制
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import hashlib
import json
import time


def _checksum(data) -> str:
    """会话数据一致性校验和"""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@dataclass
class Config:
    """配置项"""
    session_timeout: float = 30.0
    max_cross_region_latency: float = 250.0
    cpu_threshold: float = 0.95
    memory_threshold: float = 0.95
    bandwidth_threshold: float = 0.98


@dataclass
class ServerNode:
    """服务器节点"""
    server_id: str
    region: str
    capacity: int = 20
    weight: float = 1.0
    cpu: float = 0.0        # 0.0 ~ 1.0
    memory: float = 0.0     # 0.0 ~ 1.0
    bandwidth: float = 0.0  # 0.0 ~ 1.0
    healthy: bool = True
    players: List[str] = field(default_factory=list)
    sessions: Dict[str, Dict] = field(default_factory=dict)

    @property
    def player_load(self) -> float:
        return len(self.players) / self.capacity if self.capacity > 0 else 1.0

    def has_capacity(self) -> bool:
        return len(self.players) < self.capacity

    def score(self) -> float:
        """多维负载评分（越低越空闲），权重越高的服务器评分越低"""
        base = (
            0.4 * self.player_load
            + 0.2 * self.cpu
            + 0.2 * self.memory
            + 0.2 * self.bandwidth
        )
        return base / max(self.weight, 1e-6)


class ServerArchitecture:
    """服务器架构与负载均衡系统核心类"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = Config(**(config or {}))
        self.servers: Dict[str, ServerNode] = {}
        self._down_servers: set = set()
        self.degraded: bool = False
        self._cross_region_syncs: Dict[str, Dict] = {}
        self._state = {}
        self._history = []

    # ------------------------------------------------------------------
    # 基础管理
    # ------------------------------------------------------------------
    def add_server(self, server_id: str, region: str, capacity: int = 20,
                   weight: float = 1.0) -> ServerNode:
        node = ServerNode(server_id=server_id, region=region,
                          capacity=capacity, weight=weight)
        self.servers[server_id] = node
        return node

    def update_metrics(self, server_id: str, cpu: Optional[float] = None,
                       memory: Optional[float] = None,
                       bandwidth: Optional[float] = None):
        node = self.servers[server_id]
        if cpu is not None:
            node.cpu = cpu
        if memory is not None:
            node.memory = memory
        if bandwidth is not None:
            node.bandwidth = bandwidth

    def update(self, dt: float):
        """每帧更新：执行健康检查"""
        self.health_check()

    def reset(self):
        """重置状态"""
        self.servers = {}
        self._down_servers = set()
        self.degraded = False
        self._cross_region_syncs = {}
        self._state = {}
        self._history = []

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_check(self) -> Dict[str, bool]:
        """根据资源指标与崩溃标记更新服务器健康状态"""
        status = {}
        for sid, node in self.servers.items():
            if sid in self._down_servers:
                node.healthy = False
            else:
                node.healthy = (
                    node.cpu < self.config.cpu_threshold
                    and node.memory < self.config.memory_threshold
                    and node.bandwidth < self.config.bandwidth_threshold
                )
            status[sid] = node.healthy
        return status

    def _available_servers(self, region: Optional[str] = None) -> List[ServerNode]:
        self.health_check()
        return [
            n for n in self.servers.values()
            if n.healthy and n.has_capacity()
            and (region is None or n.region == region)
        ]

    # ------------------------------------------------------------------
    # 负载均衡：多维指标 + 权重
    # ------------------------------------------------------------------
    def load_balancing(self, region: Optional[str] = None) -> Optional[str]:
        """在健康且未满的服务器中，按多维加权评分选出最空闲的一台"""
        candidates = self._available_servers(region)
        if not candidates:
            return None
        best = min(candidates, key=lambda n: (n.score(), n.server_id))
        return best.server_id

    # ------------------------------------------------------------------
    # 区域路由：延迟 + 负载 + 备用区域回退
    # ------------------------------------------------------------------
    def region_routing(self, player_id: str, player_region: str,
                       latencies: Optional[Dict[str, float]] = None,
                       session_data: Optional[Dict] = None,
                       now: Optional[float] = None) -> Optional[str]:
        """为玩家选择服务器。

        按实测延迟对区域排序（无延迟数据时玩家所在区域优先），
        首选区域满载/不可用时自动回退到次近区域。
        """
        regions = {n.region for n in self.servers.values()}
        if not regions:
            return None

        def region_latency(region: str) -> float:
            if latencies and region in latencies:
                return latencies[region]
            return 0.0 if region == player_region else float("inf")

        for region in sorted(regions, key=lambda r: (region_latency(r), r)):
            server_id = self.load_balancing(region)
            if server_id is not None:
                self._assign_player(server_id, player_id, session_data, now)
                return server_id
        return None

    def _assign_player(self, server_id: str, player_id: str,
                       session_data: Optional[Dict] = None,
                       now: Optional[float] = None):
        node = self.servers[server_id]
        if player_id not in node.players:
            node.players.append(player_id)
        data = session_data if session_data is not None else {}
        node.sessions[player_id] = {
            "data": data,
            "checksum": _checksum(data),
            "created_at": now if now is not None else time.time(),
            "version": 1,
        }

    def _find_session(self, player_id: str):
        for sid, node in self.servers.items():
            if player_id in node.sessions:
                return sid, node
        return None, None

    # ------------------------------------------------------------------
    # 会话迁移：数据迁移 + 超时 + 一致性校验
    # ------------------------------------------------------------------
    def session_migration(self, player_id: str,
                          target_server_id: Optional[str] = None,
                          now: Optional[float] = None) -> Dict:
        """把玩家会话迁移到目标服务器（默认由负载均衡选择）。"""
        now = now if now is not None else time.time()
        source_id, source = self._find_session(player_id)
        if source is None:
            return {"success": False, "reason": "session_not_found"}

        session = source.sessions[player_id]
        if now - session["created_at"] > self.config.session_timeout:
            # 超时：清理过期会话，拒绝迁移
            source.sessions.pop(player_id, None)
            if player_id in source.players:
                source.players.remove(player_id)
            return {"success": False, "reason": "session_expired"}

        if target_server_id is None:
            target_server_id = self.load_balancing()
        if target_server_id is None or target_server_id == source_id:
            return {"success": False, "reason": "no_target_available"}
        target = self.servers[target_server_id]
        if not target.healthy or not target.has_capacity():
            return {"success": False, "reason": "target_unavailable"}

        # 迁移数据并做一致性校验
        migrated = {
            "data": dict(session["data"]),
            "checksum": session["checksum"],
            "created_at": session["created_at"],
            "version": session["version"] + 1,
        }
        if _checksum(migrated["data"]) != session["checksum"]:
            return {"success": False, "reason": "consistency_error"}

        target.sessions[player_id] = migrated
        target.players.append(player_id)
        source.sessions.pop(player_id, None)
        if player_id in source.players:
            source.players.remove(player_id)
        return {
            "success": True,
            "player_id": player_id,
            "source": source_id,
            "target": target_server_id,
            "version": migrated["version"],
        }

    def reconnect(self, player_id: str, now: Optional[float] = None) -> Dict:
        """断线重连：会话仍在时迁移到可用服务器继续比赛，不丢数据"""
        now = now if now is not None else time.time()
        source_id, _ = self._find_session(player_id)
        if source_id is None:
            return {"success": False, "reason": "session_not_found"}
        result = self.session_migration(player_id, now=now)
        if not result["success"] and result["reason"] == "no_target_available":
            # 只有原服务器可用时直接保留在原服务器
            return {"success": True, "player_id": player_id,
                    "source": source_id, "target": source_id,
                    "version": self.servers[source_id].sessions[player_id]["version"]}
        return result

    # ------------------------------------------------------------------
    # 故障转移：健康检查 + 会话迁移 + 降级方案
    # ------------------------------------------------------------------
    def failover(self, server_id: str, now: Optional[float] = None) -> Dict:
        """服务器崩溃后的故障转移：把会话迁移到健康服务器"""
        now = now if now is not None else time.time()
        node = self.servers[server_id]
        self._down_servers.add(server_id)
        node.healthy = False

        migrated, failed = [], []
        for player_id in list(node.sessions.keys()):
            target_id = self.load_balancing()
            if target_id is None:
                failed.append(player_id)
                continue
            result = self.session_migration(player_id, target_id, now=now)
            (migrated if result["success"] else failed).append(player_id)

        if failed:
            # 降级方案：没有健康服务器可接收时进入降级模式，
            # 保留会话数据等待恢复，而不是直接让玩家掉线丢数据
            self.degraded = True
        return {
            "server_id": server_id,
            "migrated": migrated,
            "failed": failed,
            "degraded": self.degraded,
        }

    def recover_server(self, server_id: str):
        """服务器恢复后退出降级模式"""
        self._down_servers.discard(server_id)
        if server_id in self.servers:
            self.servers[server_id].healthy = True
        self.degraded = not any(self._available_servers())

    # ------------------------------------------------------------------
    # 跨区对战：区域限制 + 延迟补偿 + 数据同步
    # ------------------------------------------------------------------
    def cross_region_play(self, player_id: str, home_region: str,
                          target_region: str,
                          latencies: Optional[Dict[str, float]] = None,
                          session_data: Optional[Dict] = None,
                          now: Optional[float] = None) -> Dict:
        """跨区加入对战。

        - 区域限制：延迟差超过 max_cross_region_latency 时拒绝
        - 延迟补偿：为低延迟一方施加补偿，拉平双方有效延迟
        - 数据同步：会话快照同步到两个区域并校验一致性
        """
        now = now if now is not None else time.time()
        latencies = latencies or {}
        home_latency = latencies.get(home_region, 0.0)
        target_latency = latencies.get(target_region, 0.0)
        latency_gap = abs(home_latency - target_latency)

        if latency_gap > self.config.max_cross_region_latency:
            return {"success": False, "reason": "region_restricted",
                    "latency_gap": latency_gap}

        server_id = self.load_balancing(target_region)
        if server_id is None:
            return {"success": False, "reason": "no_capacity"}

        self._assign_player(server_id, player_id, session_data, now)

        # 延迟补偿：给低延迟方增加一半差值的补偿延迟
        compensation = latency_gap / 2.0
        effective = max(home_latency, target_latency)

        # 数据同步：在两个区域各保存一份带版本与校验和的快照
        data = session_data if session_data is not None else {}
        snapshot = {
            "data": data,
            "checksum": _checksum(data),
            "synced_at": now,
        }
        self._cross_region_syncs[player_id] = {
            "regions": {home_region: dict(snapshot), target_region: dict(snapshot)},
            "version": 1,
        }
        consistent = (
            self._cross_region_syncs[player_id]["regions"][home_region]["checksum"]
            == self._cross_region_syncs[player_id]["regions"][target_region]["checksum"]
        )
        return {
            "success": True,
            "player_id": player_id,
            "server_id": server_id,
            "latency_compensation": compensation,
            "effective_latency": effective,
            "data_synced": consistent,
        }

    def verify_cross_region_sync(self, player_id: str) -> bool:
        """校验跨区数据一致性"""
        sync = self._cross_region_syncs.get(player_id)
        if not sync:
            return False
        checksums = {r["checksum"] for r in sync["regions"].values()}
        datas = {_checksum(r["data"]) for r in sync["regions"].values()}
        return len(checksums) == 1 and checksums == datas
