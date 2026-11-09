"""Adjacent table fields must remain distinguishable in acquired source text."""
from __future__ import annotations

import pytest

from codey.research.extract import extract_text


def test_food_name_description_and_shop_keep_their_cell_boundaries() -> None:
    html = ("<table><tr><th>美食</th><th>特色</th><th>推薦店家</th></tr>"
            "<tr><td>南煎肝</td><td>豬肝嫩滑，醬汁香濃</td><td>聚春園</td></tr></table>")
    lines = [line for line in extract_text(html).splitlines() if line.strip()]
    assert lines == ["美食", "特色", "推薦店家", "南煎肝", "豬肝嫩滑，醬汁香濃", "聚春園"]


@pytest.mark.parametrize("cell", ["td", "th"])
def test_adjacent_table_fields_do_not_form_a_different_literal(cell: str) -> None:
    html = f"<table><tr><{cell}>12</{cell}><{cell}>34</{cell}></tr></table>"
    assert extract_text(html).split() == ["12", "34"]


def test_inline_markup_keeps_a_single_name_inside_its_cell() -> None:
    text = extract_text("<table><tr><td>南煎<strong>肝</strong></td><td>豬肝嫩滑</td></tr></table>")
    assert text.split() == ["南煎肝", "豬肝嫩滑"]


def test_hidden_cells_do_not_leak_into_visible_table_fields() -> None:
    html = ('<table><tr><td>南煎肝</td><td hidden>舊店名</td>'
            '<td style="display:none">隱藏說明</td><td>聚春園</td></tr></table>')
    assert extract_text(html).split() == ["南煎肝", "聚春園"]


def test_plain_inline_text_is_not_split_into_individual_fragments() -> None:
    assert extract_text("<p>福州<strong>魚丸</strong>和肉燕。</p>") == "福州魚丸和肉燕。"
