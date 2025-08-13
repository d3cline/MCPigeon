# campaigns/urls.py
from django.urls import path
from . import views

urlpatterns = [

    path("t/<str:token>/r/<int:recipient_id>/", views.track_redirect, name="campaign_track"),
    path("o/<int:message_id>/p.png", views.pixel, name="campaign_pixel"),
    path("unsub/<int:recipient_id>/", views.unsub, name="campaign_unsub"),
]
