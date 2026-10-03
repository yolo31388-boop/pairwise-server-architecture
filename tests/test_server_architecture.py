"""
服务器架构与负载均衡系统 - 验收测试

运行方式：python -m pytest tests/test_server_architecture.py -q
共 16 个测试用例
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from network.server_architecture import ServerArchitecture


class TestServerArchitecture:
    """服务器架构与负载均衡系统测试套件"""

    def setup_method(self):
        """每个测试前的初始化"""
        self.system = ServerArchitecture()

    # ------------------------------------------------------------------
    # 区域路由：延迟 + 负载 + 备用方案
    # ------------------------------------------------------------------
    def test_case_01(self):
        """区域路由按实际延迟选择最近区域，而不是只看IP归属"""
        self.system.add_server("asia-1", "asia")
        self.system.add_server("na-1", "na")
        # 玩家IP在北美，但实测到亚洲延迟更低
        server_id = self.system.region_routing(
            "p1", "na", latencies={"asia": 40.0, "na": 500.0})
        assert server_id == "asia-1"

    def test_case_02(self):
        """区域路由考虑服务器负载：满载区域不应继续塞人"""
        self.system.add_server("asia-1", "asia", capacity=1)
        self.system.add_server("eu-1", "eu")
        assert self.system.region_routing("p1", "asia") == "asia-1"
        # asia 已满，第二个玩家应回退到次近区域而不是报错或超载
        server_id = self.system.region_routing(
            "p2", "asia", latencies={"asia": 20.0, "eu": 80.0})
        assert server_id == "eu-1"

    def test_case_03(self):
        """首选区域满载时有备用方案，按延迟顺序回退到次近区域"""
        self.system.add_server("asia-1", "asia", capacity=1)
        self.system.add_server("eu-1", "eu")
        self.system.add_server("na-1", "na")
        self.system.region_routing("p1", "asia")
        server_id = self.system.region_routing(
            "p2", "asia",
            latencies={"asia": 20.0, "eu": 60.0, "na": 120.0})
        assert server_id == "eu-1"  # 次近区域优先于更远区域

    def test_case_04(self):
        """所有区域都满载时返回 None，而不是把玩家硬塞进满员服务器"""
        self.system.add_server("asia-1", "asia", capacity=1)
        self.system.region_routing("p1", "asia")
        assert self.system.region_routing("p2", "asia") is None
        assert len(self.system.servers["asia-1"].players) == 1

    # ------------------------------------------------------------------
    # 负载均衡：多维指标 + 权重
    # ------------------------------------------------------------------
    def test_case_05(self):
        """负载均衡综合CPU/内存/带宽，而不是只看在线人数"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        # s1 人数少但资源耗尽，s2 人数多但资源空闲
        self.system.region_routing("p1", "asia")  # 进入某台服务器
        self.system.update_metrics("s1", cpu=0.95, memory=0.9, bandwidth=0.9)
        self.system.update_metrics("s2", cpu=0.1, memory=0.1, bandwidth=0.1)
        assert self.system.load_balancing("asia") == "s2"

    def test_case_06(self):
        """负载均衡严格尊重容量上限，20人服务器绝不塞第21人"""
        self.system.add_server("s1", "asia", capacity=2)
        self.system.add_server("s2", "asia", capacity=2)
        for i in range(4):
            assert self.system.region_routing(f"p{i}", "asia") is not None
        assert self.system.region_routing("p5", "asia") is None
        for node in self.system.servers.values():
            assert len(node.players) <= node.capacity

    def test_case_07(self):
        """负载均衡支持权重：高权重（新）服务器承担更多负载"""
        self.system.add_server("old", "asia", weight=1.0)
        self.system.add_server("new", "asia", weight=4.0)
        assignments = [self.system.region_routing(f"p{i}", "asia")
                       for i in range(10)]
        new_count = assignments.count("new")
        old_count = assignments.count("old")
        assert new_count > old_count

    # ------------------------------------------------------------------
    # 会话迁移：数据迁移 + 超时 + 一致性
    # ------------------------------------------------------------------
    def test_case_08(self):
        """会话迁移把完整数据搬到新服务器，玩家无需重开比赛"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        data = {"equipment": ["sword", "shield"], "gold": 500, "level": 30}
        self.system.region_routing("p1", "asia", session_data=data)
        result = self.system.session_migration("p1", "s2")
        assert result["success"]
        assert self.system.servers["s2"].sessions["p1"]["data"] == data
        assert "p1" not in self.system.servers["s1"].sessions

    def test_case_09(self):
        """会话迁移有超时：过期会话不允许迁移并被清理"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        self.system.region_routing("p1", "asia", now=1000.0)
        # 超过默认 30s 超时
        result = self.system.session_migration("p1", "s2", now=1000.0 + 31.0)
        assert not result["success"]
        assert result["reason"] == "session_expired"
        assert "p1" not in self.system.servers["s1"].sessions

    def test_case_10(self):
        """会话迁移做一致性校验：校验和损坏时拒绝迁移且保留原数据"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        self.system.region_routing("p1", "asia",
                                   session_data={"gold": 500})
        # 模拟数据损坏：数据被篡改但校验和未更新
        self.system.servers["s1"].sessions["p1"]["data"]["gold"] = 999999
        result = self.system.session_migration("p1", "s2")
        assert not result["success"]
        assert result["reason"] == "consistency_error"
        assert "p1" in self.system.servers["s1"].sessions

    def test_case_11(self):
        """断线重连后会话迁移到新服务器，装备和经济数据不丢失"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        data = {"equipment": ["sword"], "gold": 500}
        self.system.region_routing("p1", "asia", session_data=data)
        self.system.failover("s1")  # 原服务器崩溃
        result = self.system.reconnect("p1")
        assert result["success"]
        assert result["target"] == "s2"
        assert self.system.servers["s2"].sessions["p1"]["data"] == data

    # ------------------------------------------------------------------
    # 故障转移：健康检查 + 迁移 + 降级
    # ------------------------------------------------------------------
    def test_case_12(self):
        """服务器崩溃后故障转移把玩家迁移到健康服务器，不直接掉线"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        self.system.region_routing("p1", "asia", session_data={"gold": 1})
        report = self.system.failover("s1")
        assert "p1" in report["migrated"]
        assert "p1" in self.system.servers["s2"].players

    def test_case_13(self):
        """健康检查：崩溃或资源耗尽的服务器不再接收新玩家"""
        self.system.add_server("s1", "asia")
        self.system.add_server("s2", "asia")
        self.system.failover("s1")
        for i in range(5):
            assert self.system.region_routing(f"p{i}", "asia") == "s2"
        # 资源超限同样被健康检查剔除
        self.system.update_metrics("s2", cpu=0.99)
        assert self.system.health_check()["s2"] is False

    def test_case_14(self):
        """全部服务器崩溃时进入降级模式：保留会话数据而不是直接丢"""
        self.system.add_server("s1", "asia")
        self.system.region_routing("p1", "asia", session_data={"gold": 1})
        report = self.system.failover("s1")
        assert report["degraded"] is True
        assert self.system.degraded is True
        # 会话数据仍保留，恢复后可继续
        assert "p1" in self.system.servers["s1"].sessions

    # ------------------------------------------------------------------
    # 跨区对战：延迟补偿 + 数据同步 + 区域限制
    # ------------------------------------------------------------------
    def test_case_15(self):
        """跨区对战施加延迟补偿，拉平双方有效延迟"""
        self.system.add_server("asia-1", "asia")
        self.system.add_server("eu-1", "eu")
        result = self.system.cross_region_play(
            "p1", "asia", "eu", latencies={"asia": 30.0, "eu": 130.0})
        assert result["success"]
        assert result["latency_compensation"] == 50.0
        assert result["effective_latency"] == 130.0

    def test_case_16(self):
        """跨区对战：数据双向同步一致，且延迟差过大时被区域限制拒绝"""
        self.system.add_server("asia-1", "asia")
        self.system.add_server("na-1", "na")
        data = {"equipment": ["sword"], "gold": 500}
        result = self.system.cross_region_play(
            "p1", "asia", "na", session_data=data,
            latencies={"asia": 30.0, "na": 120.0})
        assert result["success"] and result["data_synced"]
        assert self.system.verify_cross_region_sync("p1")
        # 延迟差超过阈值，拒绝跨区
        rejected = self.system.cross_region_play(
            "p2", "asia", "na", latencies={"asia": 20.0, "na": 500.0})
        assert not rejected["success"]
        assert rejected["reason"] == "region_restricted"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
