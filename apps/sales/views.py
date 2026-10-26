from datetime import date as date_cls, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.decorators import live_permission_required, permission_required
from apps.accounts.models import User
from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import SalesChannel, Setting

from . import search, services
from .forms import AddToCartForm, CheckoutForm, CloseShiftForm, OpenShiftForm, QuantityForm
from .models import Payment, Sale, Shift

SELL = "sales.counter_sell"
COUNTER = SalesChannel.INDEPENDENT_COUNTER


# ---------------------------------------------------------------- the billing screen
def _counter_page(request, checkout_form=None):
    user = request.user
    shift = services.current_shift(user)
    ctx = {"shift": shift, "open_form": OpenShiftForm()}
    if shift:
        draft = services.get_draft(user)
        cart = search.cart_context(draft)
        q = request.GET.get("q", "").strip()
        ctx.update(
            cart=cart, draft=draft, summary=services.shift_summary(shift), q=q,
            line_paise=",".join(str(int(p["line_total"] * 100)) for row in cart["rows"] for p in row["parts"]),
            results=search.search_items(q) if q else [], discount_limit=services.discount_limit(user),
            checkout=checkout_form or CheckoutForm(initial={
                "sale_id": draft.pk if draft else "", "pay1_method": "CASH",
                "pay1_amount": cart["subtotal"] if cart["rows"] else "", "expected_subtotal": cart["subtotal"]}))
    return render(request, "sales/counter.html", ctx)


@permission_required(SELL)
def counter(request):
    return _counter_page(request)


@live_permission_required(SELL)
def search_partial(request):
    q = request.GET.get("q", "").strip()
    return render(request, "sales/_results.html", {"results": search.search_items(q) if q else [], "q": q})


def _act(request, call, success=""):
    try:
        call()
    except services.SaleError as exc:
        messages.error(request, str(exc))
    else:
        if success:
            messages.success(request, success)
    return redirect("sales:counter")


@permission_required(SELL)
@require_POST
def cart_add(request):
    form = AddToCartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a quantity of 1 or more.")
        return redirect("sales:counter")
    return _act(request, lambda: services.add_to_cart(
        actor=request.user, request=request, medicine_id=form.cleaned_data["medicine_id"],
        quantity=form.cleaned_data["quantity"]))


@permission_required(SELL)
@require_POST
def cart_quantity(request, pk):
    form = QuantityForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a whole number.")
        return redirect("sales:counter")
    return _act(request, lambda: services.set_quantity(
        actor=request.user, request=request, line_id=pk, quantity=form.cleaned_data["quantity"]))


@permission_required(SELL)
@require_POST
def cart_remove(request, pk):
    return _act(request, lambda: services.remove_line(actor=request.user, request=request, line_id=pk))


@permission_required(SELL)
@require_POST
def cart_discard(request):
    return _act(request, lambda: services.discard_draft(actor=request.user, request=request), "Bill discarded.")


@permission_required(SELL)
@require_POST
def checkout(request):
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        return _counter_page(request, checkout_form=form)
    d = form.cleaned_data
    try:
        sale = services.complete_counter_sale(
            actor=request.user, request=request, sale_id=d["sale_id"], payments=form.payments(),
            discount_percent=d["discount_percent"] or 0, discount_reason=d["discount_reason"],
            customer_name=d["customer_name"], customer_phone=d["customer_phone"],
            expected_subtotal=d["expected_subtotal"], restricted=form.restricted())
    except services.SaleError as exc:
        messages.error(request, str(exc))
        return _counter_page(request, checkout_form=form)
    messages.success(request, f"Sale {sale.number} completed.")
    return redirect("sales:receipt", pk=sale.pk)


# ---------------------------------------------------------------- receipt
def _may_see(user, sale):
    return user_has_permission(user, "sales.view_all") or (
        user_has_permission(user, "sales.view_own") and sale.cashier_id == user.pk)


@login_required
def receipt(request, pk):
    sale = get_object_or_404(
        Sale.objects.select_related("cashier", "rx_approved_by").prefetch_related("lines", "payments"),
        pk=pk, channel=COUNTER, status=Sale.Status.COMPLETED)
    if not _may_see(request.user, sale):
        log_event(user=request.user, action="auth.permission_denied", summary=f"receipt {sale.pk}", request=request)
        raise PermissionDenied
    earlier_prints = sale.receipt_prints
    Sale.objects.filter(pk=sale.pk).update(receipt_prints=F("receipt_prints") + 1)  # a counter, not a financial field
    if earlier_prints:
        log_event(user=request.user, action="sale.receipt_reprint", obj=sale, request=request)
    return render(request, "sales/receipt.html", {
        "sale": sale, "duplicate": earlier_prints > 0, "a4": request.GET.get("size") == "a4",
        "footer": Setting.get("receipt_footer_text", "Prices are MRP, inclusive of applicable taxes."),
        "can_sell": user_has_permission(request.user, SELL)})


# ---------------------------------------------------------------- sales lists
def _day(request, name):
    try:
        return date_cls.fromisoformat(request.GET.get(name, ""))
    except ValueError:
        return None


def _sales_list(request, scope):
    today = timezone.localdate()
    start, end = _day(request, "from") or today, _day(request, "to") or today
    tz = timezone.get_current_timezone()
    sales = Sale.objects.filter(
        channel=COUNTER, status=Sale.Status.COMPLETED,
        completed_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz),
        completed_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))
    cashiers = []
    if scope == "own":
        sales = sales.filter(cashier=request.user)
    else:
        cashiers = User.objects.filter(role__in=[User.Role.COUNTER, User.Role.PHARMACY_MANAGER]).order_by("username")
        if request.GET.get("cashier", "").isdigit():
            sales = sales.filter(cashier_id=int(request.GET["cashier"]))
    sales = sales.select_related("cashier").prefetch_related("payments")
    by_method = (Payment.objects.filter(sale__in=sales).values("method", "kind").annotate(total=Sum("amount")))
    return render(request, "sales/sales_list.html", {
        "sales": sales[:500], "scope": scope, "start": start, "end": end, "cashiers": cashiers,
        "cashier": request.GET.get("cashier", ""), "total": sales.aggregate(t=Sum("total"))["t"] or 0,
        "by_method": list(by_method)})


@permission_required("sales.view_own")
def my_sales(request):
    return _sales_list(request, "own")


@permission_required("sales.view_all")
def all_sales(request):
    return _sales_list(request, "all")


# ---------------------------------------------------------------- shift
@permission_required(SELL)
def shift_page(request):
    shift = services.current_shift(request.user)
    return render(request, "sales/shift.html", {
        "shift": shift, "summary": services.shift_summary(shift) if shift else None, "open_form": OpenShiftForm(),
        "close_form": CloseShiftForm(),
        "recent": Shift.objects.filter(user=request.user, desk="COUNTER", closed_at__isnull=False)[:10]})


@permission_required(SELL)
@require_POST
def shift_open(request):
    form = OpenShiftForm(request.POST)
    if form.is_valid():
        try:
            services.open_shift(actor=request.user, request=request, opening_cash=form.cleaned_data["opening_cash"])
            messages.success(request, "Shift opened. You can start billing.")
            return redirect("sales:counter")
        except services.SaleError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Enter the opening cash as a number.")
    return redirect("sales:shift")


@permission_required(SELL)
@require_POST
def shift_close(request):
    shift = services.current_shift(request.user)
    form = CloseShiftForm(request.POST)
    if shift is None:
        messages.error(request, "You have no open shift.")
    elif form.is_valid():
        try:
            closed = services.close_shift(actor=request.user, request=request, shift_id=shift.pk,
                                          counted_cash=form.cleaned_data["counted_cash"], notes=form.cleaned_data["notes"])
            messages.success(request, f"Shift closed. Expected cash \u20b9{closed.expected_cash}, counted \u20b9{closed.counted_cash}, "
                                      f"difference \u20b9{closed.variance}.")
        except services.SaleError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Enter the cash you counted.")
    return redirect("sales:shift")
