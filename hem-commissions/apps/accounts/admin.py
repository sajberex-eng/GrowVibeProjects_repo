from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from .models import City, Department, Position, User


class UserCreationAdminForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username",)


class UserChangeAdminForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserChangeAdminForm
    add_form = UserCreationAdminForm
    list_display = ("username", "full_name", "position", "department", "city", "email", "is_active", "is_staff")
    list_filter = ("is_active", "is_staff", "city", "department")
    search_fields = ("username", "last_name", "first_name", "middle_name", "email", "phone")
    list_select_related = ("position", "department", "city")
    ordering = ("last_name", "first_name")
    readonly_fields = ("last_login", "date_joined", "failed_login_attempts")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Персональные данные", {"fields": ("last_name", "first_name", "middle_name", "email", "phone")}),
        ("Работа", {"fields": ("position", "department", "city", "dismissed_at")}),
        ("Уведомления", {"fields": ("whatsapp_opt_in",)}),
        ("Безопасность", {"fields": ("must_change_password", "failed_login_attempts", "locked_until")}),
        ("Права", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "usable_password", "password1", "password2"),
        }),
        ("Персональные данные", {"fields": ("last_name", "first_name", "middle_name", "email")}),
        ("Работа", {"fields": ("position", "department", "city")}),
    )

    @admin.display(description="ФИО")
    def full_name(self, obj):
        return obj.full_name


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    search_fields = ("name",)
