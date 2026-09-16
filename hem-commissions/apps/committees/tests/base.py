import shutil
import tempfile

from django.test import TestCase, override_settings

from apps.committees.testing import make_commission, make_user


class MediaTestCase(TestCase):
    """Тесты с временным MEDIA_ROOT."""

    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.mkdtemp(prefix="hc-test-media-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)


class CommissionTestCase(MediaTestCase):
    def setUp(self):
        self.commission, info = make_commission(members=3)
        self.chairman = info["chairman"]
        self.secretary = info["secretary"]
        self.members = info["members"]
        self.member = self.members[0]
        self.base_order = info["order"]
        self.admin = make_user(admin=True)
        self.outsider = make_user()
