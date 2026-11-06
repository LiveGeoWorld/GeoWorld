"""UserInterfaceAppRuntime — the user <-> agent message channel.

Every task enters through ``send_message_to_agent`` (user instruction) and is
ultimately answered through ``send_message_to_user`` (the agent's committed
final answer). The latter is a USER_OUTPUT operation: it is the user-facing
counterpart to a tool-level commit (e.g. ``MapApp.select_grounded_location``),
and the judge checks both the tool decision *and* this answer to detect
"answer-fitting" where a model states a correct answer without grounding it in
real app evidence.

The runtime keeps an ordered conversation log so later tasks (multi-turn,
clarification, notifications) can build on the same channel.
"""
from __future__ import annotations
from typing import Any
from app.runtime.base import OperationType, RuntimeApp, app_tool

class UserInterfaceAppRuntime(RuntimeApp):
    """Stateful message channel between the user (scenario) and the agent."""
    name = 'UserInterfaceApp'

    def __init__(self) -> None:
        self.conversation: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        super().__init__()

    @app_tool(OperationType.USER_INPUT, summary='用户向 Agent 发送指令')
    def send_message_to_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        content = str(args.get('content', ''))
        message = {'role': 'user', 'content': content}
        if args.get('input_assets'):
            message['input_assets'] = args['input_assets']
        self.conversation.append(message)
        return {'status': 'delivered'}

    @app_tool(OperationType.USER_OUTPUT, summary='Agent 向用户发送最终答复')
    def send_message_to_user(self, args: dict[str, Any]) -> dict[str, Any]:
        content = str(args.get('content', ''))
        message: dict[str, Any] = {'role': 'agent', 'content': content}
        if args.get('attachment'):
            message['attachment'] = args['attachment']
        self.conversation.append(message)
        return {'status': 'sent', 'message': {'content': content}}

    @app_tool(OperationType.READ, summary='读取会话历史')
    def get_conversation_history(self, args: dict[str, Any]) -> dict[str, Any]:
        return {'messages': list(self.conversation)}

    @app_tool(OperationType.READ, summary='接收系统通知')
    def receive_notification(self, args: dict[str, Any]) -> dict[str, Any]:
        return {'notifications': list(self.notifications)}

    def push_notification(self, notification: dict[str, Any]) -> None:
        self.notifications.append(dict(notification))

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.conversation):
            if message.get('role') == 'user':
                return str(message.get('content', ''))
        return ''

    @property
    def final_answer(self) -> str:
        for message in reversed(self.conversation):
            if message.get('role') == 'agent':
                return str(message.get('content', ''))
        return ''

    def get_state(self) -> dict[str, Any]:
        return {'conversation': list(self.conversation), 'notifications': list(self.notifications)}

    def load_state(self, state: dict[str, Any]) -> None:
        self.conversation = list(state.get('conversation', []))
        self.notifications = list(state.get('notifications', []))

    def reset(self) -> None:
        super().reset()
        self.conversation.clear()
        self.notifications.clear()
