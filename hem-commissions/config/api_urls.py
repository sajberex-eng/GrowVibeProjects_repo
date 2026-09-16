from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.api import views

router = DefaultRouter()
router.register("commissions", views.CommissionViewSet, basename="commission")
router.register("members-history", views.MemberRecordViewSet, basename="member-record")
router.register("orders", views.OrderViewSet, basename="order")
router.register("legal-acts", views.LegalActViewSet, basename="legal-act")
router.register("meetings", views.MeetingViewSet, basename="meeting")
router.register("votings", views.VotingSessionViewSet, basename="voting")
router.register("protocols", views.ProtocolViewSet, basename="protocol")
router.register("assignments", views.AssignmentViewSet, basename="assignment")

urlpatterns = [
    path("", include(router.urls)),
]
