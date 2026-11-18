from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import TemplateView

from community import views as community
from community import ban_views

urlpatterns = [
    path("", community.home, name="home"),
    path("matches/", community.matches, name="matches"),
    path("matches/<int:match_id>/", community.match_detail, name="match"),
    path("ranking/", community.ranking, name="ranking"),
    path("bans/", ban_views.banlist, name="banlist"),
    path("bans/tickets/", ban_views.unban_tickets, name="unban-tickets"),
    path("bans/tickets/new/", ban_views.create_unban_ticket, name="unban-ticket-create"),
    path("bans/tickets/<int:ticket_id>/", ban_views.unban_ticket, name="unban-ticket"),
    path("bans/tickets/<int:ticket_id>/review/", ban_views.review_unban_ticket, name="unban-ticket-review"),
    path("players/<slug:nickname>/", community.player_profile, name="player-profile"),
    path("about/", TemplateView.as_view(template_name="community/about.html"), name="about"),
    path("profile/", community.profile, name="profile"),
    path("profile/login/", auth_views.LoginView.as_view(
        template_name="community/login.html", next_page="profile", redirect_authenticated_user=True,
    ), name="profile-login"),
    path("profile/logout/", auth_views.LogoutView.as_view(next_page="home"), name="profile-logout"),
    path("profile/steam/", TemplateView.as_view(template_name="community/steam.html"), name="steam-login"),
    path("admin/", admin.site.urls),
    path("anticheat/login/", auth_views.LoginView.as_view(template_name="anticheat/login.html"), name="login"),
    path("anticheat/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("anticheat/", include("anticheat.urls")),
]
