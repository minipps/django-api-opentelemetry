"""Optional Django Ninja API used by Ninja-specific tests.

Importing this module requires the ``ninja`` extra.  It is deliberately absent
from ``tests.project.urls`` so plain-Django tests never import Ninja.
"""

from __future__ import annotations

from typing import Any

from ninja import NinjaAPI, Router, Schema
from ninja.errors import HttpError
from pydantic import Field


class ItemIn(Schema):
    name: str
    quantity: int = Field(gt=0)


class ItemOut(Schema):
    item_id: int
    name: str
    quantity: int


class CustomError(Exception):
    """Application error with a custom Ninja handler."""


api = NinjaAPI(
    title="Django Ninja OpenTelemetry test API",
    version="1.0.0",
    urls_namespace="tests_project",
)

router = Router()
nested_router = Router()


@api.get("/success")
def success(request: Any) -> dict[str, str]:
    return {"status": "ok"}


@api.get("/async-success")
async def async_success(request: Any) -> dict[str, str]:
    return {"status": "ok", "mode": "async"}


@api.get("/items/{item_id}", response=ItemOut)
def get_item(request: Any, item_id: int, quantity: int = 1) -> ItemOut:
    return ItemOut(item_id=item_id, name="item", quantity=quantity)


@api.post("/items", response={201: ItemOut})
def create_item(request: Any, item: ItemIn) -> tuple[int, ItemOut]:
    return 201, ItemOut(item_id=1, name=item.name, quantity=item.quantity)


@api.get("/search")
def search(request: Any, limit: int) -> dict[str, int]:
    return {"limit": limit}


@api.get("/error")
def unexpected_error(request: Any) -> None:
    raise RuntimeError("ninja unexpected failure")


@api.get("/custom-error")
def custom_error(request: Any) -> None:
    raise CustomError("custom failure")


@api.exception_handler(CustomError)
def handle_custom_error(request: Any, exc: CustomError) -> Any:
    return api.create_response(request, {"detail": str(exc)}, status=409)


@api.get("/http-error")
def expected_http_error(request: Any) -> None:
    raise HttpError(404, "missing item")


@api.get("/output-error", response=ItemOut)
def output_error(request: Any) -> dict[str, Any]:
    # Intentionally invalid for the declared response schema.
    return {"item_id": "not-an-int", "name": "item", "quantity": 1}


@router.get("/ping")
def router_ping(request: Any) -> dict[str, str]:
    return {"ping": "pong"}


@nested_router.get("/deep")
def nested_deep(request: Any) -> dict[str, int]:
    return {"depth": 2}


router.add_router("/nested", nested_router)
api.add_router("/router", router)
