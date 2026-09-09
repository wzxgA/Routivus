"""Safe, user-facing Skill errors."""


class SkillError(Exception):
    category = "skill_error"


class SkillConfigError(SkillError):
    category = "configuration"


class SkillParseError(SkillError):
    category = "invalid_skill"


class SkillSecurityError(SkillError):
    category = "security_rejected"


class SkillNotFoundError(SkillError):
    category = "not_found"


class SkillDisabledError(SkillError):
    category = "disabled"


class SkillContentError(SkillError):
    category = "content_limit"


def user_error(error: Exception) -> str:
    if isinstance(error, SkillSecurityError):
        return f"为安全原因拒绝加载 Skill：{error}"
    if isinstance(error, SkillNotFoundError):
        return f"Skill 不存在：{error}"
    if isinstance(error, SkillDisabledError):
        return f"Skill 未启用：{error}"
    if isinstance(error, SkillParseError):
        return f"Skill 格式无效：{error}"
    if isinstance(error, SkillContentError):
        return f"Skill 内容超过限制：{error}"
    if isinstance(error, SkillConfigError):
        return f"Skill 配置错误：{error}"
    return f"Skill 加载失败：{type(error).__name__}"
