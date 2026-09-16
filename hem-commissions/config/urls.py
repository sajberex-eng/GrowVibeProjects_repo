from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.committees import views_home

admin.site.site_header = "Комиссии Центра гематологии — администрирование"
admin.site.site_title = "Комиссии ЦГ"
admin.site.index_title = "Справочники и настройки"

urlpatterns = [
    path("", views_home.dashboard, name="dashboard"),
    path("search/", views_home.search, name="search"),
    path("accounts/", include("apps.accounts.urls")),
    path("commissions/", include("apps.committees.urls")),
    path("meetings/", include("apps.meetings.urls")),
    path("votings/", include("apps.voting.urls")),
    path("protocols/", include("apps.protocols.urls")),
    path("verify/", include("apps.protocols.verify_urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("reports/", include("apps.reports.urls")),
    path("audit/", include("apps.audit.urls")),
    path("api/", include("config.api_urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="api-schema"), name="api-docs"),
    path("admin/", admin.site.urls),
]

handler403 = "apps.committees.views_home.error_403"
handler404 = "apps.committees.views_home.error_404"
