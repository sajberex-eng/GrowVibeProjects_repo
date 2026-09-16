import re
import shutil
import tempfile
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.committees.testing import make_commission, make_meeting, make_user
from apps.meetings.models import AgendaItem, Attendance

from .. import services
from ..models import Assignment, Decision, Protocol, ProtocolItem

_MEDIA = tempfile.mkdtemp(prefix="protocols-tests-")


@override_settings(MEDIA_ROOT=_MEDIA, SITE_URL="http://testserver")
class ProtocolTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.commission, people = make_commission(members=3, approval_days=3)
        self.chairman = people["chairman"]
        self.secretary = people["secretary"]
        self.members = people["members"]
        self.outsider = make_user()
        self.admin = make_user(admin=True)

    # --- фабрики ---
    def make_meeting_with_agenda(self, commission=None, titles=("Об утверждении плана работы", "Разное")):
        commission = commission or self.commission
        meeting = make_meeting(commission, days=-1, place="Конференц-зал")
        for n, title in enumerate(titles, 1):
            AgendaItem.objects.create(meeting=meeting, position=n, title=title, description=f"Пояснение {n}", speaker=self.secretary)
        for user in [self.chairman, self.secretary, *self.members[:2]]:
            Attendance.objects.create(meeting=meeting, user=user, status=Attendance.Status.PRESENT)
        return meeting

    def make_draft(self, with_assignment=True):
        meeting = self.make_meeting_with_agenda()
        protocol = services.create_draft_from_meeting(meeting, self.secretary)
        item = protocol.items.first()
        item.resolved = "Утвердить план работы."
        item.save()
        if with_assignment:
            decision = Decision.objects.create(protocol=protocol, item=item, number="1.1", text="Утвердить план")
            assignment = Assignment.objects.create(
                decision=decision, text="Подготовить отчёт", responsible=self.members[0],
                due_date=timezone.localdate() + timedelta(days=10),
            )
            assignment.co_executors.add(self.members[1])
        return protocol

    def make_signing(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        for row in services.current_approvals(protocol):
            services.respond_approval(protocol, row.user, "agreed")
        services.move_to_signing(protocol, self.secretary)
        protocol.refresh_from_db()
        return protocol

    def sign_as(self, protocol, user):
        services.request_sign_code(protocol, user)
        return services.sign(protocol, user, self.last_code(user), "127.0.0.1")

    def make_signed(self):
        protocol = self.make_signing()
        self.sign_as(protocol, self.secretary)
        self.sign_as(protocol, self.chairman)
        protocol.refresh_from_db()
        return protocol

    # --- помощники ---
    @staticmethod
    def last_code(user):
        for message in reversed(mail.outbox):
            if user.email in message.to:
                m = re.search(r"Код подтверждения подписи: (\d{6})", message.subject)
                if m:
                    return m.group(1)
        raise AssertionError("Код не найден в почте")

    def login(self, user):
        self.client.force_login(user)

    def item(self, protocol, position=1):
        return ProtocolItem.objects.get(protocol=protocol, position=position)

    def reload(self, obj):
        return type(obj).objects.get(pk=obj.pk)


__all__ = ["ProtocolTestCase", "Protocol"]
