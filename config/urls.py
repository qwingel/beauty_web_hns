from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import TemplateView

from accounts import steam_login
from community import views as community
from community import ban_views

bans_prefix = settings.COMMUNITY_BANS_PREFIX

urlpatterns = [
    path("", community.home, name="home"),
    path("matches/", community.matches, name="matches"),
    path("matches/<int:match_id>/", community.match_detail, name="match"),
    path("ranking/", community.ranking, name="ranking"),
    path(f"{bans_prefix}/", ban_views.banlist, name="banlist"),
    path(f"{bans_prefix}/tickets/", ban_views.unban_tickets, name="unban-tickets"),
    path(f"{bans_prefix}/tickets/new/", ban_views.create_unban_ticket, name="unban-ticket-create"),
    path(f"{bans_prefix}/tickets/<int:ticket_id>/", ban_views.unban_ticket, name="unban-ticket"),
    path(f"{bans_prefix}/tickets/<int:ticket_id>/review/", ban_views.review_unban_ticket, name="unban-ticket-review"),
    path("players/<str:steam_id>/", community.player_profile, name="player-profile"),
    path("about/", TemplateView.as_view(template_name="community/about.html"), name="about"),
    path("profile/", community.profile, name="profile"),
    path("profile/login/", steam_login.SiteLoginView.as_view(
        template_name="community/login.html", next_page="profile", redirect_authenticated_user=True,
    ), name="profile-login"),
    path("profile/logout/", auth_views.LogoutView.as_view(next_page="home"), name="profile-logout"),
    path("profile/steam/", steam_login.start, name="steam-login"),
    path("profile/steam/return/", steam_login.complete, name="steam-return"),
    path("admin/", admin.site.urls),
    path("anticheat/login/", steam_login.SiteLoginView.as_view(template_name="anticheat/login.html"), name="login"),
    path("anticheat/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("anticheat/", include("anticheat.urls")),
]
