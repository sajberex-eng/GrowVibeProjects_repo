from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.committees.models import Commission
from apps.meetings.models import Meeting
from apps.protocols.models import Protocol


class SeedDemoContentTests(TestCase):
    def test_creates_signed_protocol_and_upcoming_meeting_per_core_commission(self):
        call_command("seed_initial", "--demo", stdout=StringIO())
        call_command("seed_demo_content", stdout=StringIO())
        for commission in Commission.objects.filter(kind=Commission.Kind.CORE):
            self.assertGreaterEqual(commission.members_on().count(), 7, commission)
            protocol = Protocol.objects.get(commission=commission)
            self.assertEqual(protocol.status, Protocol.Status.SIGNED)
            self.assertEqual(protocol.active_signatures().count(), 2)
            self.assertTrue(protocol.signed_hash)
            upcoming = Meeting.objects.get(commission=commission, status=Meeting.Status.PLANNED)
            self.assertEqual(upcoming.agenda_status, Meeting.AgendaStatus.APPROVED)
            self.assertTrue(upcoming.agenda_items.filter(is_control_item=True).exists())

        # Повторный запуск ничего не дублирует.
        call_command("seed_demo_content", stdout=StringIO())
        self.assertEqual(Protocol.objects.count(), 7)
