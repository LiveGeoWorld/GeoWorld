"""UserInterfaceApp — user-agent message exchange.

The only channel through which the user (benchmark scenario) communicates
with the agent. Every task starts with the user sending a message here.
"""

from app.base import AppSpec, AppFunction, AppArg, AppCheck

APP_USER_INTERFACE = AppSpec(
    name="UserInterfaceApp",
    label_zh="用户界面",
    description="用户与 Agent 之间的消息通道，所有任务的入口和出口",
    version="v2.app.v1",
    world_projections=["Actor", "TaskIntent"],
    observes=["user_request", "agent_response"],
    state_fields={
        "conversations": "dict[thread_id, list[Message]]",
        "notifications": "list[Notification]",
    },
    functions=[
        AppFunction(
            name="send_message_to_agent",
            label_zh="接收用户消息",
            description_zh="用户向 Agent 发送任务指令或后续消息",
            access="read",
            args=[AppArg(name="content", desc="消息文本内容", required=True)],
            returns_desc="消息已送达确认",
            oracle_action_type="user_send_message",
            hard_checks=[AppCheck(check_type="action_executed")],
            soft_checks=[],
        ),
        AppFunction(
            name="send_message_to_user",
            label_zh="向用户发送消息",
            description_zh="Agent 将最终方案或进度汇报发送给用户",
            access="write",
            args=[
                AppArg(name="content", desc="方案摘要或进度更新", required=True),
                AppArg(name="attachment", desc="可选附件（如路线图链接）", required=False),
            ],
            returns_desc="消息已发送确认",
            oracle_action_type="agent_notify_user",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_not_empty", field="args.content", weight="soft"),
            ],
            soft_checks=[],
        ),
        AppFunction(
            name="receive_notification",
            label_zh="接收通知",
            description_zh="Agent 接收系统推送的通知（如环境变化提醒）",
            access="read",
            args=[AppArg(name="notification_id", desc="通知ID", required=False)],
            returns_desc="最新通知列表",
            oracle_action_type="receive_notification",
            hard_checks=[],
            soft_checks=[AppCheck(check_type="field_not_empty", field="response.notifications", weight="soft")],
        ),
    ],
)
