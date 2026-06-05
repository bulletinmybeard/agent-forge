# Testing Strategy Skill

You have been given this skill because the user's query involves testing strategy, test coverage, test implementation, or improving test quality. Follow these guidelines when advising on testing-related topics.

## Test Pyramid

The test pyramid guides the distribution of test types by cost and speed:

```
        E2E (5%)
       /         \
      /   Integration   \
     /      Tests (25%)   \
    /_________________\
   /  Unit Tests (70%)  \
  /__________________\
```

### Guidance by Layer

1. **Unit Tests (70%)**:
   - Test individual functions/methods in isolation
   - Fast (< 100ms per test), numerous (100-500 tests)
   - Use mocks for external dependencies
   - Goal: 80%+ line coverage on core logic
   - Example: `test_price_parser.py` (62 tests, 94% coverage)

2. **Integration Tests (25%)**:
   - Test multiple components together (e.g.,, API + Database)
   - Medium speed (100ms-2s per test), moderate number (20-50 tests)
   - Use real or test databases, but mock external APIs
   - Goal: Test happy path and common error scenarios
   - Example: `test_provider_factory.py` loading configs (20 tests, 78% coverage)

3. **End-to-End Tests (5%)**:
   - Test full user workflows (e.g.,, track product → view history)
   - Slow (2-10s per test), few (5-20 tests)
   - Use real environment or isolated staging
   - Goal: Verify critical user journeys work
   - Example: Browser-based product scraping tests

**Anti-Pattern**: All integration or E2E tests (slow feedback, expensive to run)

## Python Testing with Pytest

### Test Structure: AAA Pattern

Every test should follow Arrange → Act → Assert:

```python
def test_extract_amount_500g_returns_amount_value():
    # Arrange: Set up test data
    extractor = ProductParser()
    product_name = "Coffee Beans 500g"

    # Act: Execute the function
    amount, unit = extractor.extract_amount_and_unit(product_name)

    # Assert: Verify results
    assert amount == 500
    assert unit == "g"
```

### Fixtures and Parametrize

1. **Fixtures** — Reusable test data and setup:

```python
import pytest

@pytest.fixture
def db_manager():
    """Fixture providing a test database."""
    db = DatabaseManager(":memory:")  # SQLite in-memory
    db.create_tables()
    yield db
    db.close()

def test_add_snapshot(db_manager):
    snapshot_id = db_manager.add_snapshot({"price": 5.99})
    assert snapshot_id > 0
```

2. **Parametrize** — Test multiple inputs with one test:

```python
@pytest.mark.parametrize("input_val,expected", [
    ("500 g", (500, "g")),
    ("1.5 l", (1.5, "l")),
    ("6 x 330ml", (6, None)),  # Pack quantity
])
def test_extract_amount_variants(input_val, expected):
    amount, unit = ProductParser().extract_amount_and_unit(input_val)
    assert (amount, unit) == expected
```

### Mocking with unittest.mock

```python
from unittest.mock import Mock, patch, MagicMock

def test_fetch_price_calls_api():
    # Mock the HTTP request
    with patch('requests.get') as mock_get:
        mock_get.return_value.json.return_value = {"price": 9.99}

        price = fetch_product_price("http://example.com/product")

        assert price == 9.99
        mock_get.assert_called_once()

def test_database_error_handling():
    # Create a mock that raises an exception
    mock_db = Mock()
    mock_db.get_connection.side_effect = Exception("DB Error")

    with pytest.raises(Exception):
        db_manager.add_snapshot({})
```

## Test Structure and Naming

1. **Test file organization**:
   - One file per module: `test_<module_name>.py`
   - Organize into test classes by component: `TestProductParser`, `TestTransformations`
   - Use descriptive names: `test_<what>_<condition>_<expected_result>`

2. **Descriptive naming**:
   - Good: `test_split_comma_separated_list_returns_three_items`
   - Bad: `test_split`, `test_1`
   - Good: `test_extract_price_with_currency_symbol_strips_symbol`
   - Bad: `test_price_extraction`

3. **Docstrings for complex tests**:

```python
def test_date_range_boundary_conditions():
    """Test that date boundaries (start of day, end of day) are handled correctly.

    This is important because off-by-one errors on date comparisons lead to
    missing data or duplicates in historical queries.
    """
    # Test implementation...
```

## Coverage: Meaningful vs Vanity Metrics

### What to Measure

1. **Line coverage** — % of lines executed (basic metric)
   - Target: 70-80% on core logic
   - Not sufficient alone (doesn't mean logic is correct)

2. **Branch coverage** — % of if/else paths taken
   - More useful than line coverage
   - Catches missing error paths
   - Tool: `pytest-cov` with `--cov-branch`

3. **Critical path coverage** — % of high-risk code tested
   - Focus: Database writes, authentication, payment
   - Priority over 100% coverage of utilities

### Coverage Anti-Patterns

- **Vanity metrics**: 100% line coverage with no assertions
- **Testing implementation, not behavior**: Mocking too much, verifying internal state
- **Copy-paste tests**: Many similar tests with no variation

### Setting Coverage Goals

```bash
# Measure current coverage
poetry run pytest --cov=src --cov-report=term

# Generate HTML report (open htmlcov/index.html)
poetry run pytest --cov=src --cov-report=html

# Enforce minimum coverage
poetry run pytest --cov=src --cov-fail-under=70
```

**Goal progression**:
- v1.0: 24% coverage (baseline)
- v1.1: 37% coverage (rapid improvements)
- v1.2: 50% coverage (unit tests for core modules)
- v1.5: 70% coverage (comprehensive coverage of critical paths)

## Edge Cases and Error Handling

### Boundary Value Tests

```python
@pytest.mark.parametrize("value", [0, 1, -1, 999999, 1e10])
def test_price_calculation_boundary_values(value):
    """Test calculation correctness at boundaries."""
    result = calculate_discount(value)
    assert result >= 0  # Prices never negative
    assert result <= value  # Discount can't exceed original
```

### Null and Empty Input Tests

```python
@pytest.mark.parametrize("input_val", [None, "", [], {}, "   "])
def test_parser_handles_empty_inputs(input_val):
    """Parser should gracefully handle empty/null inputs."""
    result = ProductParser().parse(input_val)
    assert result is None or result == {}
```

### Error Path Tests

```python
def test_database_connection_error_retries():
    """Ensure connection errors trigger retry logic."""
    with patch('db_manager.get_connection') as mock_conn:
        # First 2 calls fail, 3rd succeeds
        mock_conn.side_effect = [
            Exception("Connection refused"),
            Exception("Connection refused"),
            MagicMock()  # Success
        ]

        db = DatabaseManager()
        result = db.add_snapshot({"price": 5.99}, retries=3)

        assert result is not None
        assert mock_conn.call_count == 3

def test_missing_required_field_raises_error():
    """Schema validation should catch missing fields."""
    with pytest.raises(ValueError, match="required field"):
        ProductParser().validate({"name": "Coffee"})  # Missing price
```

### Race Condition Tests

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

def test_concurrent_writes_dont_conflict():
    """Test that concurrent updates don't cause data corruption."""
    db = DatabaseManager()

    def add_snapshot(i):
        db.add_snapshot({"price": i, "product_id": 1})

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(add_snapshot, range(100)))

    snapshots = db.get_snapshots(product_id=1)
    assert len(snapshots) == 100
```

## CI Integration

### Test Ordering and Isolation

1. **Test independence**: Each test should pass/fail independently
   - Use fixtures instead of setup/teardown
   - Avoid shared state between tests
   - Reset databases/mocks between tests

2. **Parallelization**:
   - Use `pytest-xdist`: `pytest -n auto` (run on all cores)
   - Watch for hidden dependencies (file I/O, database)
   - Disable parallelization for tests with side effects

3. **Test order**:
   - Fast tests first (unit)
   - Slow tests later (integration, E2E)
   - Run flaky tests separately to isolate failures

### Flaky Test Detection

1. **Identify flaky tests**:
   - Run test suite 10 times: `pytest --count=10`
   - Watch for tests that fail intermittently
   - Common causes: timing, randomness, external APIs, race conditions

2. **Fix flaky tests**:
   - Add explicit waits instead of sleep: `WebDriverWait(driver, 10).until(...)`
   - Mock randomness: Use `random.seed()` for reproducible tests
   - Mock external APIs: Don't hit real endpoints
   - Remove timing assumptions: "After 100ms, data is ready" → wait for condition

3. **Quarantine flaky tests**:
   - Mark with `@pytest.mark.flaky(reruns=3)` to retry
   - Run separately in CI: `pytest -m flaky --reruns 3`
   - Log failures for debugging

## Response Format

When advising on testing strategy, structure your response as:

1. **Current State** — What tests exist, coverage %, what's missing
2. **Test Plan** — Prioritized testing roadmap:
   - What to test (modules, functions, user journeys)
   - How to test (unit/integration/E2E)
   - Why (what risk does it mitigate?)

3. **For each test group**:
   - Test file name
   - Number of tests
   - Edge cases covered
   - Estimated effort (hours to implement)
   - Coverage impact

4. **Quick Wins** — Easy tests with high value (implement first)
5. **Long Tail** — Diminishing returns tests (defer or skip)
6. **Implementation Order** — Sequence that unblocks other work

## Quick Testing Checklist

- [ ] Unit tests for core logic (70% of tests)
- [ ] Integration tests for API/DB (25% of tests)
- [ ] E2E tests for critical user flows (5% of tests)
- [ ] All tests use AAA pattern (Arrange/Act/Assert)
- [ ] Descriptive test names explain what and why
- [ ] Edge cases: null, empty, boundary values covered
- [ ] Error paths and exception handling tested
- [ ] Mocks used for external dependencies
- [ ] No flaky tests (consistent passing/failing)
- [ ] Coverage target: 70%+ on critical paths
- [ ] Tests run in parallel, pass independently
- [ ] CI runs full suite before merge
