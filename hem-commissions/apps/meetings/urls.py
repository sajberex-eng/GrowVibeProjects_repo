from django.urls import path

from . import views

app_name = "meetings"

urlpatterns = [
    path("", views.meeting_list, name="list"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("new/", views.meeting_create, name="create"),
    path("my.ics", views.my_ics, name="my_ics"),
    path("commission/<int:commission_pk>/calendar.ics", views.commission_ics, name="commission_ics"),
    path("commission/<int:commission_pk>/schedule/", views.schedule_view, name="schedule"),
    path("<int:pk>/", views.meeting_detail, name="detail"),
    path("<int:pk>/edit/", views.meeting_edit, name="edit"),
    path("<int:pk>/reschedule/", views.meeting_reschedule, name="reschedule"),
    path("<int:pk>/cancel/", views.meeting_cancel, name="cancel"),
    path("<int:pk>/held/", views.meeting_mark_held, name="mark_held"),
    path("<int:pk>/meeting.ics", views.meeting_ics, name="ics"),
    path("<int:pk>/agenda.docx", views.agenda_docx, name="agenda_docx"),
    # повестка
    path("<int:pk>/agenda/add/", views.item_add, name="item_add"),
    path("<int:pk>/agenda/control/", views.control_refresh, name="control_refresh"),
    path("<int:pk>/agenda/submit/", views.agenda_submit, name="agenda_submit"),
    path("<int:pk>/agenda/approve/", views.agenda_approve, name="agenda_approve"),
    path("<int:pk>/agenda/return/", views.agenda_return, name="agenda_return"),
    path("<int:pk>/agenda/acknowledge/", views.acknowledge, name="acknowledge"),
    path("agenda/<int:item_pk>/edit/", views.item_edit, name="item_edit"),
    path("agenda/<int:item_pk>/delete/", views.item_delete, name="item_delete"),
    path("agenda/<int:item_pk>/move/<str:direction>/", views.item_move, name="item_move"),
    # предложения
    path("<int:pk>/proposals/new/", views.proposal_create, name="proposal_create"),
    path("proposals/<int:proposal_pk>/accept/", views.proposal_accept, name="proposal_accept"),
    path("proposals/<int:proposal_pk>/reject/", views.proposal_reject, name="proposal_reject"),
    path("proposals/<int:proposal_pk>/file/", views.proposal_file, name="proposal_file"),
    # материалы
    path("<int:pk>/materials/add/", views.material_add, name="material_add"),
    path("materials/<int:material_pk>/delete/", views.material_delete, name="material_delete"),
    path("materials/<int:material_pk>/download/", views.material_download, name="material_download"),
    path("materials/<int:material_pk>/transcribe/", views.transcribe, name="transcribe"),
    # участники
    path("<int:pk>/attendance/", views.attendance, name="attendance"),
    path("<int:pk>/invitations/add/", views.invitation_add, name="invitation_add"),
    path("invitations/<int:invitation_pk>/delete/", views.invitation_delete, name="invitation_delete"),
    # транскрипты
    path("<int:pk>/transcripts/new/", views.transcript_manual, name="transcript_manual"),
    path("transcripts/<int:transcript_pk>/", views.transcript_view, name="transcript_view"),
    path("transcripts/<int:transcript_pk>/edit/", views.transcript_edit, name="transcript_edit"),
    path("transcripts/<int:transcript_pk>/delete/", views.transcript_delete, name="transcript_delete"),
]
