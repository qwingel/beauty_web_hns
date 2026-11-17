from django.urls import path

from . import views

app_name = "anticheat"
urlpatterns = [
    path("", views.index, name="index"),
    path("players/<uuid:player_id>/", views.player_detail, name="player"),
    path("connections/<uuid:segment_id>/", views.segment_detail, name="segment"),
    path("connections/<uuid:segment_id>/windows/", views.windows, name="windows"),
    path("connections/<uuid:segment_id>/windows/<int:window_id>/", views.windows, name="window"),
]
