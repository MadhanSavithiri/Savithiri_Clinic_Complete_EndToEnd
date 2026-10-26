from django import template

from apps.accounts.permissions import user_has_permission

register = template.Library()


@register.filter
def can(user, code):
    """{% if request.user|can:"accounts.manage_users" %}  (hides UI only; views still enforce)"""
    return user_has_permission(user, code)
