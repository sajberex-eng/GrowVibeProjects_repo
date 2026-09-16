from apps.committees import access
from apps.committees.models import CommissionAccess
from apps.committees.testing import make_user

from .base import CommissionTestCase


class AccessRolesTests(CommissionTestCase):
    def test_roles(self):
        c = self.commission
        self.assertEqual(access.roles_in(self.chairman, c), {access.CHAIRMAN})
        self.assertEqual(access.roles_in(self.secretary, c), {access.SECRETARY})
        self.assertEqual(access.roles_in(self.member, c), {access.MEMBER})
        self.assertEqual(access.roles_in(self.outsider, c), set())
        self.assertIn(access.ADMIN, access.roles_in(self.admin, c))

    def test_manage_and_view(self):
        c = self.commission
        for user in (self.chairman, self.secretary, self.admin):
            self.assertTrue(access.can_manage(user, c))
        self.assertFalse(access.can_manage(self.member, c))
        self.assertTrue(access.can_view(self.member, c))
        self.assertFalse(access.can_view(self.outsider, c))

    def test_observer(self):
        boss = make_user()
        CommissionAccess.objects.create(commission=self.commission, user=boss, granted_by=self.admin)
        self.assertEqual(access.roles_in(boss, self.commission), {access.OBSERVER})
        self.assertTrue(access.can_view(boss, self.commission))
        self.assertFalse(access.can_manage(boss, self.commission))
        self.assertIn(self.commission, access.visible_commissions(boss))
        self.assertNotIn(self.commission, access.visible_commissions(self.outsider))
