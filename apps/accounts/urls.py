from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.StaffLoginView.as_view(), name="login"),
    path("logout/", views.StaffLogoutView.as_view(), name="logout"),
    path("password/change/", views.password_change, name="password_change"),
    path("staff/", views.user_list, name="user_list"),
    path("staff/new/", views.user_create, name="user_create"),
    path("staff/<int:pk>/", views.user_detail, name="user_detail"),
    path("staff/<int:pk>/edit/", views.user_edit, name="user_edit"),
    path("staff/<int:pk>/reset-password/", views.user_reset_password, name="user_reset_password"),
    path("staff/<int:pk>/active/", views.user_toggle_active, name="user_toggle_active"),
    path("staff/<int:pk>/unlock/", views.user_unlock, name="user_unlock"),
]
