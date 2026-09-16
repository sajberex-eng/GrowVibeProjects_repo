"""Фабрики для тестов всех приложений."""
from datetime import date, timedelta
from itertools import count

from django.utils import timezone

from apps.accounts.models import City, Position, User

from .models import Commission, MemberRecord, Order

_seq = count(1)


def make_user(username=None, admin=False, **kwargs):
    n = next(_seq)
    username = username or f"user{n}"
    defaults = {
        "first_name": kwargs.pop("first_name", f"Имя{n}"),
        "last_name": kwargs.pop("last_name", f"Фамилия{n}"),
        "email": kwargs.pop("email", f"{username}@example.kz"),
        "must_change_password": False,
        "is_staff": admin,
    }
    defaults.update(kwargs)
    user = User(username=username, **defaults)
    user.set_password("Passw0rd!Test")
    user.save()
    return user


def make_commission(short_name=None, members=3, start=None, **kwargs):
    """Комиссия с председателем, секретарём и `members` членами.

    Возвращает (commission, dict) где dict содержит chairman, secretary, members, order.
    """
    n = next(_seq)
    short_name = short_name or f"К{n}"
    commission = Commission.objects.create(name=kwargs.pop("name", f"Комиссия {short_name}"), short_name=short_name, **kwargs)
    city, _ = City.objects.get_or_create(name="Астана")
    commission.cities.add(city)
    start = start or date.today() - timedelta(days=365)
    order = Order.objects.create(
        commission=commission, number=f"{n}-од", signed_on=start, status=Order.Status.SIGNED, title="О составе"
    )
    position, _ = Position.objects.get_or_create(name="Врач-гематолог")
    chairman = make_user(position=position, city=city)
    secretary = make_user(position=position, city=city)
    plain = [make_user(position=position, city=city) for _ in range(members)]
    MemberRecord.objects.create(commission=commission, user=chairman, role=MemberRecord.Role.CHAIRMAN, start_date=start, start_order=order)
    MemberRecord.objects.create(commission=commission, user=secretary, role=MemberRecord.Role.SECRETARY, start_date=start, start_order=order)
    for u in plain:
        MemberRecord.objects.create(commission=commission, user=u, role=MemberRecord.Role.MEMBER, start_date=start, start_order=order)
    return commission, {"chairman": chairman, "secretary": secretary, "members": plain, "order": order}


def make_meeting(commission, days=7, **kwargs):
    from apps.meetings.models import Meeting

    starts_at = kwargs.pop("starts_at", timezone.now() + timedelta(days=days))
    return Meeting.objects.create(commission=commission, starts_at=starts_at, **kwargs)
