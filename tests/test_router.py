"""SmartRouter 路由核心单元测试。

断言依据：Routivus-docs/smart-docs/ADAPTIVE_ROUTING.md §6.1 验证表与 §7 后处理规则。
"""

from __future__ import annotations

from routivus.router import (
    TIER_NAMES,
    extract,
    postprocess,
    resolve,
    route,
    rule_route,
    rule_score,
)
from routivus.router.features import code_blocks
from routivus.router.keywords import KEYWORDS
from routivus.router.postprocess import hit
from routivus.router.rule_router import confidence

from tests.conftest import seed_config


class TestKeywords:
    def test_categories_present(self):
        assert set(KEYWORDS) == {
            "arch", "risk", "planning", "impl", "teach",
            "constraint", "chatty", "debug",
        }

    def test_bilingual(self):
        assert "架构" in KEYWORDS["arch"]
        assert "architecture" in KEYWORDS["arch"]

    def test_hit_case_insensitive(self):
        assert hit("Please DEPLOY now", "risk")
        assert not hit("hello world only", "risk")


class TestFeatures:
    def test_basic_counts(self):
        f = extract("写个二分查找")
        assert f["len_chars"] == len("写个二分查找")
        assert f["num_impl_kw"] == 1          # "写"
        assert f["num_code_blocks"] == 0
        assert f["is_chatty"] == 0

    def test_chatty_flag(self):
        assert extract("你好")["is_chatty"] == 1
        assert extract("帮我写个函数")["is_chatty"] == 0

    def test_question_mark(self):
        assert extract("这是什么？")["question_mark"] == 1
        assert extract("what is this?")["question_mark"] == 1
        assert extract("写个函数")["question_mark"] == 0

    def test_fenced_code_blocks(self):
        text = "说明：\n```python\nprint(1)\n```\n再补充\n```json\n{\"a\": 1}\n```"
        assert len(code_blocks(text)) == 2
        f = extract(text)
        assert f["num_code_blocks"] == 2
        assert f["num_json"] == 1
        assert f["code_chars_ratio"] > 0

    def test_indented_code_block_fallback(self):
        text = "看这段：\n    print(1)\n    print(2)\n没了"
        assert len(code_blocks(text)) == 1

    def test_lists(self):
        f = extract("步骤：\n1. 第一步\n2. 第二步\n- 无序项")
        assert f["num_lists"] == 3

    def test_arch_and_risk_counts(self):
        f = extract("把这个模块重构为微服务并评估对下游影响")
        assert f["num_arch_kw"] == 2          # "重构" + "微服务"
        f2 = extract("设计日活千万的推荐系统架构")
        assert f2["num_planning_kw"] == 1     # "设计"
        assert f2["num_arch_kw"] == 1         # "架构"


class TestRuleRouter:
    """对照 ADAPTIVE_ROUTING §6.1 验证表逐条断言（rule_route 层）。"""

    def test_chatty_basic(self):
        assert rule_route(extract("你好")).tier_idx == 0

    def test_simple_task_enhanced(self):
        assert rule_route(extract("写个二分查找")).tier_idx == 1

    def test_teach_enhanced(self):
        assert rule_route(extract("解释下装饰器")).tier_idx == 1

    def test_arch_hard_rule_superior(self):
        assert rule_route(extract("把这个模块重构为微服务并评估对下游影响")).tier_idx == 2

    def test_design_superior(self):
        d = rule_route(extract("设计日活千万的推荐系统架构"))
        assert d.tier_idx == 2
        assert d.score == 7.0                 # planning=1 (+4) + arch=1 (+3)

    def test_long_design_ultimate(self):
        text = ("设计日活千万的推荐系统架构，包含离线训练、在线推理、"
                "AB实验三大部分，并给出性能与成本取舍")
        assert rule_route(extract(text)).tier_idx == 3

    def test_risk_forces_superior(self):
        assert rule_route(extract("生产环境部署出错了")).tier_idx == 2

    def test_many_code_blocks_ultimate(self):
        text = "分析：\n```python\na=1\n```\n```python\nb=2\n```\n```python\nc=3\n```"
        assert rule_route(extract(text)).tier_idx == 3

    def test_score_formula(self):
        f = extract("你好")
        assert rule_score(f) == -6.0          # is_chatty 扣 6 分

    def test_hard_rule_confidence_is_one(self):
        assert confidence(rule_route(extract("你好"))) == 1.0

    def test_soft_rule_confidence_range(self):
        d = rule_route(extract("设计日活千万的推荐系统架构"))
        c = confidence(d)
        assert 0.5 <= c <= 1.0


class TestPostprocess:
    def _f(self, text):
        return extract(text)

    def test_risk_flag_forces_superior(self):
        # 闲聊+风险词：规则层落 Basic，后处理风险旗标顶到 Superior
        assert postprocess(0, "帮我部署到生产环境", self._f("帮我部署到生产环境")) == 2

    def test_long_text_forces_superior(self):
        text = "描述" * 3001                   # >6000 字符
        assert postprocess(0, text, self._f(text)) == 2

    def test_big_code_block_forces_superior(self):
        text = "看代码：\n```python\n" + "x = 1\n" * 400 + "```"
        assert postprocess(0, text, self._f(text)) == 2

    def test_context_tokens_forces_superior(self):
        assert postprocess(0, "继续", self._f("继续"), context_tokens=2500) == 2

    def test_arch_flag_upgrades_one_tier(self):
        # "设计日活千万的推荐系统架构" 规则层 Superior(2)，架构旗标 → Ultimate
        assert postprocess(2, "设计日活千万的推荐系统架构",
                           self._f("设计日活千万的推荐系统架构")) == 3

    def test_debug_flag_upgrades_one_tier(self):
        assert postprocess(1, "为什么报错了", self._f("为什么报错了")) == 2

    def test_debug_upgrade_capped_at_ultimate(self):
        assert postprocess(3, "还是不行，继续排查异常", self._f("还是不行，继续排查异常")) == 3

    def test_chatty_flag_forces_basic(self):
        assert postprocess(2, "你好", self._f("你好")) == 0

    def test_chatty_with_teach_word_not_forced_basic(self):
        # 闲聊词 + 教学词并存：不触发简短闲聊旗标
        assert postprocess(2, "解释一下，谢谢", self._f("解释一下，谢谢")) >= 1

    def test_anti_downgrade_within_window(self):
        # 上一轮 Superior(2)，本轮路由 Basic(0)：600s 内最多降 1 档 → Enhanced(1)
        assert postprocess(0, "你好", self._f("你好"),
                           prev_tier=2, prev_ts=100.0, ts=200.0) == 1

    def test_downgrade_allowed_after_window(self):
        # 超过 600s 窗口：允许自由降档
        assert postprocess(0, "你好", self._f("你好"),
                           prev_tier=2, prev_ts=100.0, ts=1000.0) == 0

    def test_no_prev_tier_no_constraint(self):
        assert postprocess(0, "你好", self._f("你好")) == 0


class TestModelTiers:
    @staticmethod
    def _manager(tmp_path, env=None):
        import json as _json

        from routivus.config.manager import ConfigManager

        user_dir = tmp_path / "user_xg"
        project_dir = tmp_path / "proj_xg"
        user_dir.mkdir(exist_ok=True)
        project_dir.mkdir(exist_ok=True)
        cfg = seed_config({})
        env = env or {}
        for name in list(cfg.get("providers", {})):
            ek = f"ROUTIVUS_{name.upper()}_API_KEY"
            if env.get(ek):
                cfg["providers"][name]["api_key"] = env[ek]
        (user_dir / "config.json").write_text(_json.dumps(cfg), encoding="utf-8")
        return ConfigManager(user_dir=user_dir, project_dir=project_dir,
                             env=dict(env), load_env=False)

    def test_fallback_when_not_configured(self):
        t = resolve(0, "deepseek", "deepseek-chat")
        assert t.tier == TIER_NAMES[0]
        assert t.provider == "deepseek"
        assert t.model == "deepseek-chat"
        assert t.configured is False

    def test_explicit_config(self):
        cfg = {"Superior": {"provider": "glm", "model": "glm-4-plus"}}
        t = resolve(2, "deepseek", "deepseek-chat", cfg)
        assert t.provider == "glm"
        assert t.model == "glm-4-plus"
        assert t.configured is True

    def test_partial_config_falls_back(self):
        cfg = {"Basic": {"model": "deepseek-chat"}}   # 只配 model
        t = resolve(0, "glm", "glm-4-flash", cfg)
        assert t.provider == "glm"                     # provider 回落
        assert t.model == "deepseek-chat"
        assert t.configured is True

    def test_provider_without_model_uses_provider_default(self, tmp_path):
        """D5：档位只配 provider 不配 model → 用该 provider 默认模型。
        需要 manager 解析 provider 的 default_model。"""
        manager = self._manager(tmp_path, env={"ROUTIVUS_GLM_API_KEY": "gk"})
        cfg = {"Basic": {"provider": "glm"}}
        t = resolve(0, "deepseek", "deepseek-chat", cfg, manager)
        assert t.provider == "glm"
        assert t.model == "glm-4-flash"                # glm 内置默认模型
        assert t.configured is True

    def test_empty_tier_falls_back_to_base(self, tmp_path):
        """D4：档位完全未配置 → 回落 base provider 及其生效模型。"""
        manager = self._manager(tmp_path, env={"ROUTIVUS_GLM_API_KEY": "gk"})
        t = resolve(3, "deepseek", "deepseek-chat", {}, manager)
        assert t.provider == "deepseek"
        assert t.model == "deepseek-chat"
        assert t.configured is False


class TestModelTiersValidation:
    """子步骤 B：接入 ConfigManager 后的 provider/API Key 校验链。"""

    @staticmethod
    def _manager(tmp_path, env=None, user_cfg=None):
        import json as _json

        from routivus.config.manager import ConfigManager

        user_dir = tmp_path / "user_xg"
        project_dir = tmp_path / "proj_xg"
        user_dir.mkdir(exist_ok=True)
        project_dir.mkdir(exist_ok=True)
        cfg = seed_config(user_cfg or {})
        env = env or {}
        # env 里 ROUTIVUS_<NAME>_API_KEY 迁到 config 的 providers.<name>.api_key
        for name in list(cfg.get("providers", {})):
            ek = f"ROUTIVUS_{name.upper()}_API_KEY"
            if env.get(ek):
                cfg["providers"][name]["api_key"] = env[ek]
        (user_dir / "config.json").write_text(
            _json.dumps(cfg), encoding="utf-8"
        )
        return ConfigManager(user_dir=user_dir, project_dir=project_dir,
                             env=dict(env), load_env=False)

    def test_valid_provider_with_key_passes(self, tmp_path):
        manager = self._manager(tmp_path, env={"ROUTIVUS_GLM_API_KEY": "gk"})
        cfg = {"Superior": {"provider": "glm", "model": "glm-4-plus"}}
        t = resolve(2, "deepseek", "deepseek-chat", cfg, manager)
        assert t.provider == "glm"
        assert t.model == "glm-4-plus"
        assert t.configured is True

    def test_provider_without_api_key_falls_back(self, tmp_path):
        manager = self._manager(tmp_path, env={})   # 无任何 key
        cfg = {"Superior": {"provider": "glm", "model": "glm-4-plus"}}
        t = resolve(2, "deepseek", "deepseek-chat", cfg, manager)
        assert t.provider == "deepseek"
        assert t.model == "deepseek-chat"
        assert t.configured is False

    def test_placeholder_api_key_falls_back(self, tmp_path):
        manager = self._manager(tmp_path, env={"ROUTIVUS_GLM_API_KEY": "sk-xxx"})
        cfg = {"Superior": {"provider": "glm", "model": "glm-4-plus"}}
        t = resolve(2, "deepseek", "deepseek-chat", cfg, manager)
        assert t.provider == "deepseek"
        assert t.configured is False

    def test_unknown_provider_falls_back(self, tmp_path):
        manager = self._manager(tmp_path, env={"ROUTIVUS_GLM_API_KEY": "gk"})
        cfg = {"Superior": {"provider": "no-such-provider", "model": "whatever"}}
        t = resolve(2, "deepseek", "deepseek-chat", cfg, manager)
        assert t.provider == "deepseek"
        assert t.configured is False

    def test_same_as_fallback_skips_validation(self, tmp_path):
        # 显式配置等于 fallback 时无需校验（active 本身可能也没 key，比如离线场景）
        manager = self._manager(tmp_path, env={})
        cfg = {"Basic": {"provider": "deepseek", "model": "deepseek-chat"}}
        t = resolve(0, "deepseek", "deepseek-chat", cfg, manager)
        assert t.configured is True

    def test_route_passes_manager_through(self, tmp_path):
        manager = self._manager(tmp_path, env={"ROUTIVUS_GLM_API_KEY": "gk"})
        cfg = {"Ultimate": {"provider": "glm", "model": "glm-4-plus"}}
        r = route("设计日活千万的推荐系统架构并给出部署回滚方案",
                  fallback_provider="deepseek", fallback_model="deepseek-chat",
                  tiers_config=cfg, manager=manager)
        assert r.provider == "glm"
        assert r.model == "glm-4-plus"
        assert r.configured is True


class TestRoute:
    def test_end_to_end_basic(self):
        r = route("你好", fallback_provider="deepseek", fallback_model="deepseek-chat")
        assert r.tier == "Basic"
        assert r.tier_idx == 0
        assert r.provider == "deepseek"
        assert r.hard_rule is True
        assert r.confidence == 1.0

    def test_end_to_end_enhanced(self):
        r = route("写个二分查找", fallback_provider="deepseek", fallback_model="deepseek-chat")
        assert r.tier == "Enhanced"

    def test_end_to_end_arch_bumped_by_postprocess(self):
        # 规则层 Superior，后处理架构旗标升到 Ultimate
        r = route("设计日活千万的推荐系统架构")
        assert r.tier == "Ultimate"

    def test_end_to_end_with_tiers_config(self):
        cfg = {"Enhanced": {"provider": "glm", "model": "glm-4-flash"}}
        r = route("写个二分查找", fallback_provider="deepseek",
                  fallback_model="deepseek-chat", tiers_config=cfg)
        assert r.tier == "Enhanced"
        assert r.provider == "glm"
        assert r.model == "glm-4-flash"
        assert r.configured is True

    def test_prev_tier_by_name(self):
        r = route("你好", prev_tier="Superior", prev_ts=100.0, ts=200.0)
        assert r.tier == "Enhanced"          # 防降级：最多降一档

    def test_prev_tier_unknown_name_ignored(self):
        r = route("你好", prev_tier="Nope", prev_ts=100.0, ts=200.0)
        assert r.tier == "Basic"

    def test_features_snapshot_in_result(self):
        r = route("写个二分查找")
        assert r.features["num_impl_kw"] == 1
