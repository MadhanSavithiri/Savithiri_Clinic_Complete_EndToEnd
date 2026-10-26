from django.urls import path

from . import views

app_name = "pharmacy"

urlpatterns = [
    path("", views.medicine_list, name="list"),
    path("new/", views.medicine_create, name="create"),
    path("<int:pk>/", views.medicine_detail, name="medicine_detail"),
    path("<int:pk>/edit/", views.medicine_edit, name="edit"),
    path("stock/", views.stock_overview, name="stock_overview"),
    path("stock/batches/", views.batch_report, name="batch_report"),
    path("stock/expiry/", views.expiry_report, name="expiry_report"),
    path("stock/ledger/", views.ledger, name="ledger"),
    path("stock/valuation/", views.valuation, name="valuation"),
    path("stock/batch/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("stock/batch/<int:pk>/adjust/", views.batch_adjust, name="batch_adjust"),
    path("stock/batch/<int:pk>/wastage/", views.batch_wastage, name="batch_wastage"),
    path("stock/batch/<int:pk>/return/", views.batch_return, name="batch_return"),
    path("stock/batch/<int:pk>/recall/", views.batch_recall, name="batch_recall"),
    path("stock/batch/<int:pk>/recall/clear/", views.batch_unrecall, name="batch_unrecall"),
    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/new/", views.supplier_create, name="supplier_create"),
    path("suppliers/<int:pk>/edit/", views.supplier_edit, name="supplier_edit"),
    path("purchases/", views.purchase_list, name="purchase_list"),
    path("purchases/new/", views.purchase_create, name="purchase_create"),
    path("purchases/report/", views.purchase_report, name="purchase_report"),
    path("purchases/<int:pk>/", views.purchase_detail, name="purchase_detail"),
    path("purchases/<int:pk>/edit/", views.purchase_edit, name="purchase_edit"),
    path("purchases/<int:pk>/items/add/", views.purchase_item_add, name="purchase_item_add"),
    path("purchases/<int:pk>/items/<int:item_pk>/remove/", views.purchase_item_remove, name="purchase_item_remove"),
    path("purchases/<int:pk>/receive/", views.purchase_receive, name="purchase_receive"),
    path("purchases/<int:pk>/discard/", views.purchase_discard, name="purchase_discard"),
]

