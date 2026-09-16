from django.urls import path

from . import views

app_name = "committees"

urlpatterns = [
    path("", views.commission_list, name="list"),
    path("new/", views.commission_edit, name="create"),
    path("legal-monitor/", views.legal_monitor, name="legal_monitor"),
    path("legal-monitor/check-all/", views.legal_check_all, name="legal_check_all"),
    path("<int:pk>/", views.commission_detail, name="detail"),
    path("<int:pk>/edit/", views.commission_edit, name="edit"),
    path("<int:pk>/settings/", views.settings_edit, name="settings_edit"),
    # состав
    path("<int:pk>/composition/changes/add/", views.change_add, name="change_add"),
    path("<int:pk>/composition/changes/<int:change_id>/cancel/", views.change_cancel, name="change_cancel"),
    # приказы
    path("<int:pk>/orders/create/", views.order_create, name="order_create"),
    path("<int:pk>/orders/<int:order_id>/draft/", views.order_draft, name="order_draft"),
    path("<int:pk>/orders/<int:order_id>/regenerate/", views.order_regenerate, name="order_regenerate"),
    path("<int:pk>/orders/<int:order_id>/scan/", views.order_scan, name="order_scan"),
    path("<int:pk>/orders/<int:order_id>/register/", views.order_register, name="order_register"),
    path("<int:pk>/orders/<int:order_id>/cancel/", views.order_cancel, name="order_cancel"),
    # положения
    path("<int:pk>/regulations/upload/", views.regulation_upload, name="regulation_upload"),
    path("<int:pk>/regulations/<int:reg_id>/download/", views.regulation_download, name="regulation_download"),
    path("<int:pk>/regulations/<int:reg_id>/expire/", views.regulation_expire, name="regulation_expire"),
    # НПА
    path("<int:pk>/legal/add/", views.legal_act_edit, name="legal_act_add"),
    path("<int:pk>/legal/check/", views.legal_check_commission, name="legal_check_commission"),
    path("<int:pk>/legal/<int:act_id>/edit/", views.legal_act_edit, name="legal_act_edit"),
    path("<int:pk>/legal/<int:act_id>/check/", views.legal_act_check, name="legal_act_check"),
    path("<int:pk>/legal/<int:act_id>/retire/", views.legal_act_retire, name="legal_act_retire"),
    path("<int:pk>/legal/<int:act_id>/replace/", views.legal_act_replace, name="legal_act_replace"),
    path("<int:pk>/legal/<int:act_id>/log/", views.legal_act_log, name="legal_act_log"),
    # рубрикатор и наблюдатели
    path("<int:pk>/rubrics/add/", views.rubric_edit, name="rubric_add"),
    path("<int:pk>/rubrics/<int:rubric_id>/edit/", views.rubric_edit, name="rubric_edit"),
    path("<int:pk>/rubrics/<int:rubric_id>/delete/", views.rubric_delete, name="rubric_delete"),
    path("<int:pk>/observers/grant/", views.observer_grant, name="observer_grant"),
    path("<int:pk>/observers/<int:access_id>/revoke/", views.observer_revoke, name="observer_revoke"),
]
