"""CommandPolicy：把一条 shell 命令分类为 (effect, retry, allowed, needs_approval)。

取代 run_command 里那条"防手滑"的极小 deny 正则。分类只描述命令的**性质**；真正的
执行门禁（推测执行期禁 external、非交互拒未知高风险、审批）在 7d 的守卫点按此决策落地。

三层：
1. DANGEROUS（`allowed=False`，永远拒）：rm -rf /、mkfs、dd of=/dev、fork bomb、
   `curl|sh`、shutdown/reboot、写裸设备。
2. EXTERNAL（`effect=EXTERNAL_SIDE_EFFECT, retry=NEVER, needs_approval=True`）：网络/发布/
   包管理/DB/远程——candidate 回滚不了，推测执行期应禁止、需审批。
3. LOCAL（默认）：只碰工作区。纯读命令 → READ_ONLY/SAFE；构建测试/改文件 → WORKSPACE_WRITE，
   幂等的 SAFE、追加类（`>>`）非幂等 NEVER。

设计取舍：**本地未识别命令默认按 WORKSPACE_WRITE 放行**（受 workspace cwd + 7c 的 env 过滤/
进程树终止约束），而不是一刀切拒绝——否则正常开发命令（echo/cat/python/git…）全被挡。
用户所说"非交互默认拒绝未知高风险"落在 EXTERNAL/DANGEROUS 这两类高风险上，而非普通本地命令。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from codeagent.tool.effects import EffectKind, RetryPolicy


@dataclass(frozen=True, slots=True)
class CommandDecision:
    allowed: bool  # False = 危险，直接拒
    effect: EffectKind
    retry: RetryPolicy
    needs_approval: bool = False  # external / 未知高风险：需审批或推测期禁止
    reason: str = ""


# 1) 危险：永远拒（比旧 _DENY 更全，但仍只挡明显灾难性操作）
_DANGEROUS = re.compile(
    r"(?:^|[\s;&|`(])(?:"
    r"rm\s+-[a-z]*r[a-z]*f?\s+/(?:\s|$|\*)"  # rm -rf /
    r"|rm\s+-[a-z]*f[a-z]*r?\s+/(?:\s|$|\*)"
    r"|mkfs\b"
    r"|dd\b[^\n]*\bof=/dev/"
    r"|>\s*/dev/[sh]d[a-z]"
    r"|:\(\)\s*\{.*\};\s*:"  # fork bomb
    r"|\b(?:shutdown|reboot|halt|poweroff)\b"
    r"|\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"  # curl ... | sh
    r")",
    re.IGNORECASE,
)

# 2) 外部副作用：网络 / 发布 / 包管理 / DB / 远程 / 服务
_EXTERNAL = re.compile(
    r"(?:^|[\s;&|`(])(?:"
    r"curl|wget|nc|netcat|telnet|ssh|scp|sftp|rsync"
    r"|pip\s+install|pip3\s+install|conda\s+install|poetry\s+add"
    r"|npm\s+(?:install|i|publish)|yarn\s+add|pnpm\s+add"
    r"|apt(?:-get)?\s+install|yum\s+install|dnf\s+install|brew\s+install|apk\s+add"
    r"|docker\b|podman\b|kubectl\b|helm\b|systemctl\b|service\b|crontab\b"
    r"|git\s+push|git\s+pull|git\s+fetch|git\s+clone"
    r"|psql|mysql|mongo|redis-cli|sqlite3\s+[^\s]+\.db"
    r"|aws\b|gcloud\b|az\b|terraform\b"
    r")",
    re.IGNORECASE,
)

# 3a) 纯读本地命令 → READ_ONLY/SAFE
_READ_ONLY = re.compile(
    r"^(?:cat|less|head|tail|ls|pwd|find|grep|rg|wc|stat|file|which|env|printenv"
    r"|git\s+(?:status|log|diff|show|branch|rev-parse|ls-files))\b",
    re.IGNORECASE,
)

# 3b) 追加重定向 → 非幂等，只能靠 Attempt 回滚，不可自动重放
_APPEND = re.compile(r">>")


class CommandPolicy:
    def __init__(
        self,
        *,
        extra_allow: list[str] | None = None,
        extra_deny: list[str] | None = None,
    ) -> None:
        self._allow = [re.compile(p, re.IGNORECASE) for p in (extra_allow or [])]
        self._deny = [re.compile(p, re.IGNORECASE) for p in (extra_deny or [])]

    def classify(self, command: str) -> CommandDecision:
        cmd = command.strip()
        if not cmd:
            return CommandDecision(False, EffectKind.READ_ONLY, RetryPolicy.SAFE, reason="空命令")
        # 配置 denylist 优先于一切
        if any(p.search(cmd) for p in self._deny):
            return CommandDecision(
                False, EffectKind.WORKSPACE_WRITE, RetryPolicy.NEVER, reason="命中配置 denylist"
            )
        if _DANGEROUS.search(cmd):
            return CommandDecision(
                False, EffectKind.WORKSPACE_WRITE, RetryPolicy.NEVER,
                reason="疑似破坏性命令",
            )
        # 配置 allowlist：显式放行为本地安全命令
        if any(p.search(cmd) for p in self._allow):
            return CommandDecision(True, EffectKind.WORKSPACE_WRITE, RetryPolicy.SAFE)
        if _EXTERNAL.search(cmd):
            return CommandDecision(
                True, EffectKind.EXTERNAL_SIDE_EFFECT, RetryPolicy.NEVER,
                needs_approval=True, reason="外部副作用（网络/发布/包管理/DB/远程）",
            )
        if _READ_ONLY.match(cmd) and not _APPEND.search(cmd):
            return CommandDecision(True, EffectKind.READ_ONLY, RetryPolicy.SAFE)
        # 本地写：追加类非幂等（NEVER），其余按幂等 SAFE 处理
        retry = RetryPolicy.NEVER if _APPEND.search(cmd) else RetryPolicy.SAFE
        return CommandDecision(True, EffectKind.WORKSPACE_WRITE, retry)


__all__ = ["CommandDecision", "CommandPolicy"]
