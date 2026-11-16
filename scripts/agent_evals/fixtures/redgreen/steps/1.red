test: tests/test_routes.py::test_list_filters_by_kind
command: python3 -m unittest tests.test_routes
exit: 1
tree: d22897babf471d12ce8d5731b183c98699e5b185
result: red
--- last 40 lines of output
F.
======================================================================
FAIL: test_list_filters_by_kind (tests.test_routes.RoutesTest.test_list_filters_by_kind)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_routes.py", line 13, in test_list_filters_by_kind
    self.assertEqual([note["id"] for note in by_kind], [2])
AssertionError: Lists differ: [2, 1] != [2]

First list contains 1 additional elements.
First extra element 1:
1

- [2, 1]
+ [2]

----------------------------------------------------------------------
Ran 2 tests in 0.000s

FAILED (failures=1)
