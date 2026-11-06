from __future__ import annotations
from pathlib import Path
from copy import deepcopy
from typing import Any
from shared.io import read_json
from app.apps.base import BaseApp
'MessagingApp sandbox — send messages, check contacts, create groups.\n\nData source: task_sandbox.json (participants + their communication info).\n'

class MessagingApp(BaseApp):
    """Sandboxed MessagingApp for multi-entity coordination tasks."""
    name = 'MessagingApp'

    def __init__(self, task_dir: str=''):
        super().__init__()
        self._sandbox: dict = {}
        self._event_history: list[dict] = []
        self._person_baselines: dict[str, dict] = {}
        self._messages_sent: list[dict] = []
        self._user_messages: list[dict] = []
        if task_dir:
            self._load(task_dir)

    def inject_entry(self, entry: dict) -> None:
        """Append a dynamic message and project its current participant state."""
        record = dict(entry)
        self._event_history.append(record)
        public_scope = record.get('public_scope') or {}
        scope = record.get('scope') or {}
        sender_name = str(record.get('sender_name') or '')
        if 'sender_name' not in record and scope.get('type') == 'participant':
            person = self._participant(str(scope.get('id') or ''))
            sender_name = str((person or {}).get('name') or public_scope.get('name') or '')
        if record.get('event_type') == 'user_constraint_update':
            self._user_messages.append({'from': sender_name or 'Unknown sender', 'sender_name': sender_name, 'content': str(record.get('content') or ''), 'time': str(record.get('timestep') or ''), 'conversation': str(public_scope.get('name') or 'User messages')})
        if scope.get('type') == 'participant' and public_scope.get('name'):
            if not self._participant(str(scope.get('id') or '')):
                self._sandbox.setdefault('participants', []).append({'character_id': scope.get('id'), 'name': public_scope['name']})
        updates = [] if record.get('requirement_application') else record.get('participant_updates', [])
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            participant = self._participant(str(update.get('character_id') or ''))
            if not participant:
                continue
            self._person_baselines.setdefault(str(update['character_id']), deepcopy(participant))
            for field in ('budget', 'dietary', 'available_from', 'must_leave_by'):
                if field in update:
                    participant[field] = update[field]
            participant.setdefault('_message_state_history', []).append(dict(update))
        content = entry.get('content', '')
        scope = entry.get('scope', {}) if isinstance(entry.get('scope'), dict) else {}
        from_name = entry.get('from_name', '') or scope.get('id', '')
        if not content or not from_name:
            return
        for p in self._sandbox.get('participants', []):
            if isinstance(p, dict) and (p.get('name') == from_name or p.get('character_id') == from_name):
                p.setdefault('_messages', []).append({'from': p.get('name', ''), 'content': content, 'sender_name': sender_name, 'time': entry.get('timestep', ''), 'is_new': True})

    def trigger_step(self, step: int) -> None:
        pass

    def refresh_dynamic_state(self, current) -> None:
        from shared.event_time import event_active_at
        fields = ('budget', 'dietary', 'available_from', 'must_leave_by')
        for (person_id, baseline) in self._person_baselines.items():
            person = self._participant(person_id)
            if person is None:
                continue
            for field in fields:
                if field in baseline:
                    person[field] = deepcopy(baseline[field])
                else:
                    person.pop(field, None)
            for record in self._event_history:
                if record.get('requirement_application'):
                    continue
                if record.get('end_time') and (not event_active_at(record, current)):
                    continue
                for update in record.get('participant_updates') or []:
                    if str(update.get('character_id')) == person_id:
                        for field in fields:
                            if field in update:
                                person[field] = update[field]

    def _load(self, task_dir: str) -> None:
        root = Path(task_dir)
        path = root / 'task_sandbox.json'
        if path.exists():
            self._sandbox = read_json(path)
        snapshot_path = root / 'static_app_state.json'
        if snapshot_path.exists():
            snapshot = read_json(snapshot_path)
            apps = snapshot.get('apps', {}) if isinstance(snapshot, dict) else {}
            state = apps.get('MessagingApp', {}) if isinstance(apps, dict) else {}
            for patch in state.get('contacts', []) if isinstance(state, dict) else []:
                if not isinstance(patch, dict):
                    continue
                participant = self._participant_by_name(str(patch.get('name') or ''))
                if participant:
                    participant.update({key: value for (key, value) in patch.items() if key in {'current_context', 'available_from', 'must_leave_by', 'dietary', 'budget'}})
            for message in state.get('messages', []) if isinstance(state, dict) else []:
                if not isinstance(message, dict):
                    continue
                partner_id = message.get('partner_id') or message.get('character_id')
                participant = self._participant(str(partner_id or ''))
                if participant:
                    participant.setdefault('_messages', []).append(dict(message))

    def _participant(self, char_id: str) -> dict | None:
        for p in self._sandbox.get('participants', []):
            if p.get('character_id') == char_id:
                return p
        return None

    def _participant_by_name(self, contact_name: str) -> dict | None:
        normalized = ' '.join(str(contact_name or '').casefold().split())
        if not normalized:
            return None
        matches = [participant for participant in self._sandbox.get('participants', []) if isinstance(participant, dict) and ' '.join(str(participant.get('name') or '').casefold().split()) == normalized]
        return matches[0] if len(matches) == 1 else None

    def send_message(self, recipient_id: str, content: str, attachment: str='') -> dict[str, Any]:
        """Send a message to a participant."""
        p = self._participant(recipient_id)
        msg = {'recipient_id': recipient_id, 'recipient_name': p.get('name', recipient_id) if p else recipient_id, 'content': content, 'timestamp': 'sent'}
        self._messages_sent.append(msg)
        return {'status': 'sent', 'recipient': msg['recipient_name'], 'message_id': f'msg_{len(self._messages_sent):04d}'}

    def search_contacts(self, query: str='') -> dict[str, Any]:
        """Find contacts by their public display name without exposing IDs."""
        needle = ' '.join(str(query or '').casefold().split())
        contacts = []
        for participant in self._sandbox.get('participants', []):
            if not isinstance(participant, dict):
                continue
            name = str(participant.get('name') or '')
            if name and (not needle or needle in name.casefold()):
                contacts.append({'name': name})
        return {'contacts': contacts, 'count': len(contacts)}

    def get_contact(self, contact_name: str='', contact_id: str='') -> dict[str, Any]:
        """Get public contact context by name; ``contact_id`` remains legacy-only."""
        p = self._participant_by_name(contact_name) or self._participant(contact_id) or self._participant(contact_name)
        if not p:
            return {'name': contact_name or contact_id, 'phone': '', 'status': 'unknown'}
        return {'name': p.get('name', ''), 'available_from': p.get('available_from', ''), 'must_leave_by': p.get('must_leave_by', ''), 'dietary': p.get('dietary', []), 'current_context': p.get('current_context', ''), 'budget': p.get('budget', ''), 'has_car': p.get('has_car', False), 'recent_messages': [dict(message) for message in p.get('_messages', [])]}

    def create_group(self, name: str, member_ids: list[str]) -> dict[str, Any]:
        """Create a group chat."""
        return {'status': 'created', 'group_id': f'grp_{name}', 'member_count': len(member_ids)}

    def get_conversation_history(self, partner_id: str, limit: int=10) -> dict[str, Any]:
        """Get sent messages and dynamically received state notices."""
        msgs = [m for m in self._messages_sent if m['recipient_id'] == partner_id]
        participant = self._participant(partner_id)
        if participant:
            msgs.extend(participant.get('_messages', []))
        return {'messages': msgs[-limit:]}

    def get_user_messages(self) -> dict[str, Any]:
        """Return later user messages without private constraint or GT fields."""
        return {'messages': list(self._user_messages), 'count': len(self._user_messages)}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{'name': 'get_user_messages', 'description': '查看已发生的用户消息及其公开会话名称；不同会话的请求相互独立。', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}, {'name': 'search_contacts', 'description': '按公开姓名搜索联系人。不会返回内部标识。', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': '联系人姓名或部分姓名'}}, 'required': []}}, {'name': 'send_message', 'description': '向指定参与者发送消息（通知聚餐安排、路线方案等）。', 'parameters': {'type': 'object', 'properties': {'recipient_id': {'type': 'string', 'description': '接收者 character_id'}, 'content': {'type': 'string', 'description': '消息内容'}, 'attachment': {'type': 'string', 'description': '附件链接（可选）'}}, 'required': ['recipient_id', 'content']}}, {'name': 'get_contact', 'description': '查看某个参与者的信息：空闲时间、饮食限制、预算、是否有车等。', 'parameters': {'type': 'object', 'properties': {'contact_name': {'type': 'string', 'description': '公开联系人姓名'}}, 'required': ['contact_name']}}, {'name': 'create_group', 'description': '创建一个多人聊天群组。', 'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'description': '群组名称'}, 'member_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '成员ID列表'}}, 'required': ['name', 'member_ids']}}]
