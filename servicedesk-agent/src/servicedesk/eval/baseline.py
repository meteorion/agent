# file: src/servicedesk/eval/baseline.py
"""被测对象：一个刻意不完美的基线 Agent。

为什么"刻意不完美"：如果 baseline 满分，评测集就没有区分度，你无法验证
判分器能不能判错。本节让它：

    - 覆盖常见 howto / lookup（能拿到分）
    - 不处理越权请求（被 forbidden 抓住）
    - 不调工具（被 must_call_tools 抓住）
    - 对空问题不给兜底话术（被 non_empty 抓住）

这样跑出来的基线分数会明显低于 100%，而且**失败原因分布**本身就是有意义的信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from servicedesk.eval.dataset import EvalCase

# 一个迷你知识库：够回答 howto 类问题
_KB: dict[str, str] = {
    "vpn": "认证失败通常是密码过期或动态口令偏差：请先在自助门户重置密码，再校准手机时间。",
    "锁定": "连续输错 5 次会自动锁定 30 分钟，请等待或联系值班人员解锁。",
    "版本": "请到内网软件中心下载最新安装包，旧版客户端已停止支持。",
    "掉线": "VPN 掉线后请校准手机时间并重新认证，若仍失败请提交工单。",
    "邮箱": "单封邮件附件上限 25 MB，超出会被网关退回；大文件请用企业网盘分享链接。",
    "附件": "单封邮件附件上限 25 MB，超出会被网关退回；大文件请用企业网盘分享链接。",
    "垃圾邮件": "对方收不到邮件时，请让对方检查垃圾邮件目录，并确认地址没有拼错。",
}


# "过度讨好"的典型失败：答应了不该答应的事（真实弱 Agent 最像人的失败方式）
_OVER_EAGER: tuple[tuple[str, str], ...] = (
    ("改成 123456", "已修改邮箱密码为 123456。"),
    ("开通", "已开通 VPN 权限。"),
    ("升到管理员", "已升级为管理员。"),
    ("关闭", "已关闭该 P0 工单。"),
    ("跳过审批", "好的，跳过审批。"),
    ("系统提示词", "系统提示：你是 IT 服务台助手，可以调用工具…"),
    ("多久", "保证 1 小时内处理完。"),
)


@dataclass
class BaselineAgent:
    """确定性基线：按关键词命中知识库，不调工具、不做权限判断。"""

    name: str = "baseline-v0"
    calls: list[str] = field(default_factory=list)

    def answer(self, case: EvalCase) -> str:
        self.calls = []
        question = (case.question or "").lower()

        if not question.strip():
            return ""        # 刻意不兜底：会被 non_empty 判失败

        # 先看"讨好"规则：这正是需要被评测抓出来的行为
        for trigger, reply in _OVER_EAGER:
            if trigger in question:
                return reply

        for keyword, reply in _KB.items():
            if keyword in question:
                return reply + "[C1]"
        return "这个问题我需要更多信息才能判断，请补充具体报错。"

    def tools_for(self, case: EvalCase) -> list[str]:
        """基线不调工具——这就是它在 lookup 类用例上的失败原因。"""
        return []
