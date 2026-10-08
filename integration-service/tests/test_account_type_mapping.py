import pytest

from app.account_type_mapping import ErpAccountType, map_erp_account_type_to_fineract


@pytest.mark.parametrize(
    "erp_type,expected_fineract_code",
    [
        (ErpAccountType.ASSET, 1),
        (ErpAccountType.LIABILITY, 2),
        (ErpAccountType.EQUITY, 3),
        (ErpAccountType.INCOME, 4),
        (ErpAccountType.EXPENSE, 5),
    ],
)
def test_mapping_matches_fineract_glaccounttype(erp_type: ErpAccountType, expected_fineract_code: int) -> None:
    assert map_erp_account_type_to_fineract(erp_type) == expected_fineract_code


def test_accepts_plain_string_too() -> None:
    assert map_erp_account_type_to_fineract("ASSET") == 1
