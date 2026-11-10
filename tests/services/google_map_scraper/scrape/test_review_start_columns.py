from app.services.google_map_scraper.scrape.lead_rows import review_start_columns


def test_histogram_is_split_into_one_count_per_star():
    columns = review_start_columns('{"1": 17, "2": 5, "3": 9, "4": 8, "5": 13}')
    assert columns == {
        "review_star_1_count": "17",
        "review_star_2_count": "5",
        "review_star_3_count": "9",
        "review_star_4_count": "8",
        "review_star_5_count": "13",
    }


def test_missing_histogram_gives_blank_counts():
    columns = review_start_columns("")
    assert set(columns.values()) == {""}
    assert len(columns) == 5


def test_malformed_histogram_gives_blank_counts():
    assert set(review_start_columns("not json").values()) == {""}
