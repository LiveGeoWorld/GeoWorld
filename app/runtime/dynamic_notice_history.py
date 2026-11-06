from copy import deepcopy
'Public notice archive attached only when a dynamic event is loaded.'

class DynamicNoticeHistory:

    def __init__(self):
        self.notices = []

    def get_notice_history(self):
        return {'notices': deepcopy(self.notices)}
