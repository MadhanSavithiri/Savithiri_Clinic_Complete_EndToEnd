from django.urls import path

from . import views

app_name = "sales"

urlpatterns = [
    path("", views.counter, name="counter"),
    path("search/", views.search_partial, name="search"),
    path("cart/add/", views.cart_add, name="cart_add"),
    path("cart/line/<int:pk>/quantity/", views.cart_quantity, name="cart_quantity"),
    path("cart/line/<int:pk>/remove/", views.cart_remove, name="cart_remove"),
    path("cart/discard/", views.cart_discard, name="cart_discard"),
    path("checkout/", views.checkout, name="checkout"),
    path("sale/<int:pk>/receipt/", views.receipt, name="receipt"),
    path("sales/", views.my_sales, name="my_sales"),
    path("all-sales/", views.all_sales, name="all_sales"),
    path("shift/", views.shift_page, name="shift"),
    path("shift/open/", views.shift_open, name="shift_open"),
    path("shift/close/", views.shift_close, name="shift_close"),
]
