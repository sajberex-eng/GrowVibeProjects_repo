import shutil
import tempfile

from django.test import TestCase, override_settings

from apps.committees.testing import make_commission, make_meeting, make_user

_MEDIA = tempfile.mkdtemp(prefix="meetings-tests-")


@override_settings(
    MEDIA_ROOT=_MEDIA,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TRANSCRIPTION_BACKEND="",
)
class MeetingTestCase(TestCase):
    members = 3
    commission_kwargs = {}

    @classmethod
    def setUpTestData(cls):
        cls.commission, people = make_commission(members=cls.members, **cls.commission_kwargs)
        cls.chairman = people["chairman"]
        cls.secretary = people["secretary"]
        cls.plain = people["members"]
        cls.member = cls.plain[0]
        cls.outsider = make_user()
        cls.admin = make_user(admin=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def meeting(self, **kwargs):
        return make_meeting(self.commission, **kwargs)

    def login(self, user):
        self.client.force_login(user)
