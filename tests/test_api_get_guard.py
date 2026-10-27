"""api_get reads inventory data only: no customers, employees or payments.

The token can read Customer and Employee (they come with one of the scopes),
so the escape hatch has to refuse them itself, before any request is made.
"""

from __future__ import annotations

import pytest

from lightspeed_mcp.reader import InventoryReader
from tests.test_reader import CannedClient


@pytest.fixture
def client():
    return CannedClient(matrix_readable=True)


@pytest.mark.parametrize("path", [
    "Customer.json",
    "Customer/13773.json",
    "Employee.json",
    "Employee/10",
    "/API/V3/Account/304581/Customer.json",  # prefixed paths are normalised first
    "Account/304581/Employee.json",
    "Sale/25262/EmailReceipt.json",  # an allowed endpoint's non-inventory sub-resource
    "Discount.json",
    "Register.json",
    "PaymentType.json",
    "CreditAccount.json",
    "Tag.json",
    "customer.json",  # Lightspeed names are case-sensitive; lowercase is not on the list either
])
def test_non_inventory_paths_are_refused_before_any_request(client, path):
    with pytest.raises(ValueError, match="only reads inventory data"):
        InventoryReader(client).api_get(path)
    with pytest.raises(ValueError):
        InventoryReader(client).api_get(path, all_pages=True)
    assert client.calls == []


@pytest.mark.parametrize("relations", [
    '["Customer"]',
    '["SaleLines","SaleLines.Employee"]',
    '["SalePayments"]',
    '["Customer.Contact"]',
    '["customer"]',
    '["Item","Customer"',  # malformed JSON is checked as text
    "Customer",
])
def test_relations_that_reach_people_or_payments_are_refused(client, relations):
    with pytest.raises(ValueError, match="kept out of this server"):
        InventoryReader(client).api_get("Sale.json", {"load_relations": relations})
    assert client.calls == []


def test_relations_given_as_a_list_are_checked_too(client):
    with pytest.raises(ValueError, match="kept out"):
        InventoryReader(client).api_get("Sale.json", {"load_relations": ["SaleLines", "Customer"]})
    assert client.calls == []


@pytest.mark.parametrize("path, params", [
    ("Item.json", {"description": "~,%navy%"}),
    ("Item/1644.json", {"load_relations": '["ItemShops","ItemAttributes","CustomFieldValues"]'}),
    ("Item/CustomField.json", None),
    ("ItemMatrix/7.json", {"load_relations": '["ItemAttributeSet"]'}),
    ("Order/853.json", {"load_relations": '["OrderLines","OrderShipments","Note"]'}),
    ("Sale.json", {"load_relations": '["SaleLines","SaleLines.Item"]'}),
    ("InventoryLog.json", {"createTime": ">=,2026-10-01T00:00:00+00:00"}),
    ("VendorReturn/1.json", {"load_relations": '["VendorReturnItems","VendorReturnItems.Item"]'}),
    ("Vendor.json", {"load_relations": '["Contact"]'}),  # a vendor's business contact is fine
    ("API/V3/Account/304581/Shop.json", None),
])
def test_inventory_paths_still_work(client, path, params):
    InventoryReader(client).api_get(path, params)
    assert len(client.calls) == 1
