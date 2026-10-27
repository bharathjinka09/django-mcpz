from __future__ import annotations

import datetime as dt
import io
from typing import Annotated, Any

import msgspec
from django.db.models import Sum
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from django_mcpz.server import (
    Icon,
    Image,
    MCPServer,
    ResourceLink,
    Text,
    ToolError,
    elicit,
    public,
)
from pizzeria.models import Order, Pizza

server = MCPServer(
    name="mcpizza",
    version="1.0.0",
    title="MCPizza",
    description="A pizza place: browse the menu, read the kitchen’s notes, and order.",
    website_url="https://github.com/adamchainz/django-mcpz/tree/main/example",
    # Shown by clients alongside the title, and on the OAuth consent page.
    # A static file, served by runserver in DEBUG mode.
    icons=[Icon(static="pizzeria/icon.svg", sizes=("any",))],
    instructions=(
        "MCPizza sells pizzas. Call current_date to learn today's date, then"
        " search_menu with on_date to see what is on the menu for a given"
        " day. Read each pizza's notes for dietary options and requests the"
        " kitchen will honour. Order with place_order, using exact pizza"
        " names, and put any requests in the requests field. If the user"
        " is unsure what to have, recommend_pizza asks them about their budget"
        " and diet and suggests one."
    ),
    # Served on localhost only, so this example skips authentication. See
    # the docs for adding a bearer token or other auth.
    auth=public,
)


@server.tool(
    description="Today's date and weekday, for working out what is on the menu.",
    read_only=True,
)
def current_date(request: HttpRequest) -> dict[str, str]:
    today = dt.date.today()
    return {"date": today.isoformat(), "weekday": today.strftime("%A")}


class SearchMenuParams(msgspec.Struct):
    query: Annotated[
        str | None,
        msgspec.Meta(description="Case-insensitive substring match on pizza names."),
    ] = None
    # The constraint sits inside the optional, since msgspec only allows ge
    # on a numeric type, not on a union with None.
    max_price: (
        Annotated[
            float,
            msgspec.Meta(description="Only pizzas costing at most this much.", ge=0.0),
        ]
        | None
    ) = None
    on_date: Annotated[
        dt.date | None,
        msgspec.Meta(
            description="The date to show the menu for, as YYYY-MM-DD. Defaults to today."
        ),
    ] = None


def _pizza_json(pizza: Pizza) -> dict[str, Any]:
    return {
        "name": pizza.name,
        "description": pizza.description,
        "price": float(pizza.price),
        "vegetarian": pizza.vegetarian,
        "notes": pizza.notes,
        "available_from": (
            pizza.available_from.isoformat() if pizza.available_from else None
        ),
        "available_until": (
            pizza.available_until.isoformat() if pizza.available_until else None
        ),
    }


@server.tool(
    description=(
        "The pizzas on the menu for a date, with prices and notes. Notes"
        " describe dietary options, requests the kitchen will honour, and"
        " warnings, so read them rather than guessing."
    ),
    read_only=True,
)
def search_menu(request: HttpRequest, params: SearchMenuParams) -> dict[str, Any]:
    on_date = params.on_date or dt.date.today()
    pizzas = Pizza.objects.all()
    if params.query is not None:
        pizzas = pizzas.filter(name__icontains=params.query)
    if params.max_price is not None:
        pizzas = pizzas.filter(price__lte=params.max_price)
    return {
        "date": on_date.isoformat(),
        "pizzas": [_pizza_json(p) for p in pizzas if p.available_on(on_date)],
    }


class PlaceOrderParams(msgspec.Struct):
    pizza: Annotated[
        str,
        msgspec.Meta(description="Exact pizza name, as returned by search_menu."),
    ]
    quantity: Annotated[int, msgspec.Meta(ge=1, le=20)] = 1
    requests: Annotated[
        str,
        msgspec.Meta(
            description=(
                "Requests for the kitchen, such as options mentioned in the"
                " pizza's notes."
            )
        ),
    ] = ""


@server.tool(description="Order a pizza from today's menu.")
def place_order(request: HttpRequest, params: PlaceOrderParams) -> dict[str, Any]:
    try:
        pizza = Pizza.objects.get(name=params.pizza)
    except Pizza.DoesNotExist:
        raise ToolError(
            f"No pizza named {params.pizza!r}. Use search_menu to find exact names."
        ) from None
    if not pizza.available_on(dt.date.today()):
        raise ToolError(f"{pizza.name} is not on today's menu.")
    order = Order.objects.create(
        pizza=pizza,
        quantity=params.quantity,
        requests=params.requests,
        total=pizza.price * params.quantity,
    )
    return {
        "order_id": order.id,
        "pizza": pizza.name,
        "quantity": order.quantity,
        "requests": order.requests,
        "total": float(order.total),
    }


class Preferences(msgspec.Struct):
    max_price: Annotated[
        float, msgspec.Meta(description="Most you want to spend, in pounds.", ge=0.0)
    ]
    vegetarian: Annotated[bool, msgspec.Meta(description="Vegetarian pizzas only?")] = (
        False
    )


@server.tool(
    description=(
        "Recommend a pizza from today's menu: the most ordered one that fits"
        " the user's budget and diet, which they are asked for."
    ),
    read_only=True,
)
def recommend_pizza(request: HttpRequest) -> dict[str, Any]:
    preferences = elicit(
        request, "What are you after? We'll pick today's favourite.", Preferences
    )
    pizzas = Pizza.objects.filter(price__lte=preferences.max_price)
    if preferences.vegetarian:
        pizzas = pizzas.filter(vegetarian=True)
    pizzas = pizzas.annotate(ordered=Sum("order__quantity", default=0)).order_by(
        "-ordered", "price"
    )
    today = dt.date.today()
    for pizza in pizzas:
        if pizza.available_on(today):
            return {**_pizza_json(pizza), "times_ordered": pizza.ordered}
    raise ToolError("Nothing on today's menu fits those preferences.")


# A categorical palette in a fixed order, checked for colour-blind readers:
# adjacent hues stay distinct under the common forms of colour blindness.
PIE_COLOURS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
SURFACE = "#fcfcfb"
INK = "#52514e"


def pie_chart_png(slices: list[tuple[str, int]]) -> bytes:
    """
    A pie chart with a legend, as PNG, drawn with Pillow.

    A raster image, rather than SVG, since that is what the assistants show
    their models. Drawn at double size and scaled down, for smooth edges.
    """
    total = sum(count for _, count in slices)
    scale = 2
    width, height = 460, max(200, 20 + 20 * len(slices))
    image = PILImage.new("RGB", (width * scale, height * scale), SURFACE)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=12 * scale)
    pie = (20 * scale, 20 * scale, 180 * scale, 180 * scale)
    start = -90.0
    for i, (label, count) in enumerate(slices):
        colour = PIE_COLOURS[i % len(PIE_COLOURS)]
        end = 270.0 if i == len(slices) - 1 else start + 360 * count / total
        # A thin gap in the surface colour separates the slices.
        draw.pieslice(pie, start, end, fill=colour, outline=SURFACE, width=2 * scale)
        start = end
        y = (20 + 20 * i) * scale
        draw.rectangle(
            (200 * scale, y - 6 * scale, 212 * scale, y + 6 * scale), fill=colour
        )
        draw.text(
            (218 * scale, y),
            f"{label}: {count} ({count / total:.0%})",
            fill=INK,
            font=font,
            anchor="lm",
        )
    image = image.resize((width, height), PILImage.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@server.tool(
    description=(
        "A pie chart of the pizzas ordered today, as an image, with the"
        " numbers behind it as text."
    ),
    read_only=True,
)
def orders_chart(request: HttpRequest) -> list[Text | Image]:
    today = timezone.localdate()
    rows = (
        Order.objects.filter(placed_at__date=today)
        .values("pizza__name")
        .annotate(quantity=Sum("quantity"))
        .order_by("-quantity", "pizza__name")
    )
    slices = [(row["pizza__name"], row["quantity"]) for row in rows]
    if not slices:
        return [Text(f"No orders yet on {today.isoformat()}.")]
    total = sum(count for _, count in slices)
    summary = ", ".join(
        f"{name} {count} ({count / total:.0%})" for name, count in slices
    )
    return [
        Text(f"Pizzas ordered on {today.isoformat()}, {total} in total: {summary}."),
        Image(pie_chart_png(slices), "image/png"),
    ]


class MenuLinkParams(msgspec.Struct):
    on_date: Annotated[
        dt.date | None,
        msgspec.Meta(
            description="The date to link the menu for, as YYYY-MM-DD. Defaults to today."
        ),
    ] = None


@server.tool(
    description=(
        "A link to the web page showing the menu for a date, past or future,"
        " for the user to open in a browser."
    ),
    read_only=True,
)
def menu_link(request: HttpRequest, params: MenuLinkParams) -> ResourceLink:
    on_date = params.on_date or dt.date.today()
    return ResourceLink(
        uri=request.build_absolute_uri(reverse("menu", args=[on_date])),
        name=f"MCPizza menu for {on_date.isoformat()}",
        mime_type="text/html",
    )
