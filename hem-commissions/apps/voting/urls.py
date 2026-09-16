from django.urls import path

from . import views

app_name = "voting"

urlpatterns = [
    path("", views.voting_list, name="list"),
    path("new/", views.voting_create, name="create"),
    path("item/<int:item_pk>/create/", views.voting_create_for_item, name="create_for_item"),
    path("absentee-protocol/", views.absentee_protocol, name="absentee_protocol"),
    path("<int:pk>/", views.voting_detail, name="detail"),
    path("<int:pk>/edit/", views.voting_edit, name="edit"),
    path("<int:pk>/materials/add/", views.material_add, name="material_add"),
    path("materials/<int:pk>/", views.material_download, name="material_download"),
    path("materials/<int:pk>/delete/", views.material_delete, name="material_delete"),
    path("<int:pk>/open/", views.voting_open, name="open"),
    path("<int:pk>/vote/", views.voting_vote, name="vote"),
    path("<int:pk>/enter-vote/", views.voting_enter, name="enter_vote"),
    path("<int:pk>/close/", views.voting_close, name="close"),
    path("<int:pk>/cancel/", views.voting_cancel, name="cancel"),
]
