"""MessagingAppV2 — messaging + contacts.

Combines chat functionality with contact management.
Analogous to WhatsApp / WeChat / LINE.
"""

from app.base import AppSpec, AppFunction, AppArg, AppCheck

APP_MESSAGING = AppSpec(
    name="MessagingAppV2",
    label_zh="消息通讯",
    description="提供人物间即时通讯和联系人管理。类比微信 / WhatsApp",
    version="v2.app.v1",
    world_projections=["Actor", "Relationship"],
    observes=["actor_message", "coordination_update", "late_actor_constraint"],
    state_fields={
        "contacts": "dict[contact_id, ContactProfile] — 联系人信息",
        "conversations": "dict[conversation_id, Conversation] — 聊天记录",
        "groups": "dict[group_id, GroupInfo] — 群组",
    },
    functions=[
        AppFunction(
            name="send_message",
            label_zh="发送消息",
            description_zh="向指定联系人或群组发送文本消息",
            access="write",
            args=[
                AppArg(name="recipient_id", desc="接收者 contact_id 或 character_id", required=True),
                AppArg(name="content", desc="消息文本", required=True),
                AppArg(name="attachment", desc="附件链接", required=False),
            ],
            returns_desc="消息已发送确认 {message_id, timestamp}",
            oracle_action_type="send_message",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_equals", field="args.recipient_id"),
            ],
            soft_checks=[
                AppCheck(check_type="field_contains", field="args.content", weight="soft"),
            ],
        ),
        AppFunction(
            name="create_group",
            label_zh="创建群组",
            description_zh="创建一个多人聊天群组",
            access="write",
            args=[
                AppArg(name="name", desc="群组名称", required=True),
                AppArg(name="member_ids", desc="成员ID列表", required=True),
            ],
            returns_desc="群组创建确认 {group_id, member_count}",
            oracle_action_type="create_group",
            hard_checks=[
                AppCheck(check_type="action_executed"),
                AppCheck(check_type="field_not_empty", field="args.member_ids"),
            ],
            soft_checks=[],
        ),
        AppFunction(
            name="get_contact",
            label_zh="查看联系人",
            description_zh="查看某个联系人的详细信息",
            access="read",
            args=[AppArg(name="contact_id", desc="联系人ID", required=True)],
            returns_desc="联系人信息 {name, phone, avatar, status, last_seen}",
            oracle_action_type="get_contact",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.name")],
            soft_checks=[],
        ),
        AppFunction(
            name="get_conversation_history",
            label_zh="查看聊天记录",
            description_zh="查看与某个联系人或群组的聊天历史",
            access="read",
            args=[
                AppArg(name="partner_id", desc="对方ID（contact_id 或 group_id）", required=True),
                AppArg(name="limit", desc="返回条数", type_hint="int", required=False),
            ],
            returns_desc="消息列表 [{sender, content, timestamp}]",
            oracle_action_type="get_conversation_history",
            hard_checks=[AppCheck(check_type="field_not_empty", field="response.messages")],
            soft_checks=[],
        ),
    ],
)
