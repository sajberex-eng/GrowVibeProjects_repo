from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.committees.forms import CompositionChangeForm
from apps.committees.models import CompositionChange, MemberRecord, Order
from apps.committees.services import composition_diff, register_signed_order
from apps.committees.testing import make_user
from apps.committees.views import composition_order

from .base import CommissionTestCase


class CompositionTests(CommissionTestCase):
    def _change(self, action, user, role="", position=""):
        return CompositionChange.objects.create(
            commission=self.commission, action=action, user=user, new_role=role, new_position=position,
        )

    def test_register_order_effective_on_signing_date(self):
        today = timezone.localdate()
        signed = today - timedelta(days=10)
        newbie = make_user()
        leaving = self.members[1]
        order = Order.objects.create(commission=self.commission, status=Order.Status.DRAFT)
        add = self._change(CompositionChange.Action.ADD, newbie, MemberRecord.Role.MEMBER)
        remove = self._change(CompositionChange.Action.REMOVE, leaving)
        promote = self._change(CompositionChange.Action.CHANGE_ROLE, self.member, MemberRecord.Role.DEPUTY)
        CompositionChange.objects.filter(pk__in=[add.pk, remove.pk, promote.pk]).update(order=order)

        register_signed_order(order, "15-од", signed)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SIGNED)
        for ch in (add, remove, promote):
            ch.refresh_from_db()
            self.assertEqual(ch.status, CompositionChange.Status.APPLIED)
            self.assertEqual(ch.applied_on, signed)

        before = signed - timedelta(days=1)
        ids_before = set(self.commission.members_on(before).values_list("user_id", flat=True))
        ids_after = set(self.commission.members_on(signed).values_list("user_id", flat=True))
        self.assertNotIn(newbie.pk, ids_before)
        self.assertIn(newbie.pk, ids_after)
        self.assertIn(leaving.pk, ids_before)
        self.assertNotIn(leaving.pk, ids_after)
        self.assertEqual(self.commission.members_on(signed).get(user=self.member).role, MemberRecord.Role.DEPUTY)
        self.assertEqual(composition_order(self.commission, today), order)
        self.assertEqual(composition_order(self.commission, before), self.base_order)

        diff = composition_diff(self.commission, before, signed)
        self.assertEqual([r.user for r in diff["added"]], [newbie])
        self.assertEqual([r.user for r in diff["removed"]], [leaving])
        self.assertEqual([p[1].user for p in diff["changed"]], [self.member])

    def test_cannot_register_twice(self):
        order = Order.objects.create(commission=self.commission, status=Order.Status.DRAFT)
        register_signed_order(order, "1", timezone.localdate())
        with self.assertRaises(ValidationError):
            register_signed_order(order, "1", timezone.localdate())

    def test_second_chairman_rejected(self):
        order = Order.objects.create(commission=self.commission, status=Order.Status.DRAFT)
        ch = self._change(CompositionChange.Action.ADD, make_user(), MemberRecord.Role.CHAIRMAN)
        ch.order = order
        ch.save()
        with self.assertRaises(ValidationError):
            register_signed_order(order, "2", timezone.localdate())
        ch.refresh_from_db()
        self.assertEqual(ch.status, CompositionChange.Status.PROJECT)

    def _form(self, **data):
        return CompositionChangeForm(data=data, commission=self.commission)

    def test_form_validation(self):
        A = CompositionChange.Action
        self.assertFalse(self._form(action=A.ADD, user=self.member.pk).is_valid())
        self.assertFalse(self._form(action=A.REMOVE, user=make_user().pk).is_valid())
        self.assertFalse(self._form(action=A.CHANGE_ROLE, user=self.member.pk).is_valid())
        self.assertFalse(self._form(action=A.UPDATE_POSITION, user=self.member.pk).is_valid())
        form = self._form(action=A.ADD, user=make_user().pk)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["new_role"], MemberRecord.Role.MEMBER)
        form.instance.commission = self.commission
        form.save()
        # повторное изменение по тому же сотруднику в проекте запрещено
        self.assertFalse(self._form(action=A.ADD, user=form.instance.user.pk).is_valid())
        self.assertTrue(self._form(action=A.REMOVE, user=self.member.pk).is_valid())
