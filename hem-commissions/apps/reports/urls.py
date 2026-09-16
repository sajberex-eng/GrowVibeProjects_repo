from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.index, name="index"),
    path("export.xlsx", views.export_xlsx, name="export_xlsx"),
    path("export.pdf", views.export_pdf, name="export_pdf"),
    path("sign/", views.sign, name="sign"),
    path("signed/", views.signed_list, name="signed_list"),
    path("signed/<int:pk>/download/", views.signed_download, name="signed_download"),
]
