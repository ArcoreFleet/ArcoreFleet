import unittest

from identifiers import oracle_identifier


class IdentifierTests(unittest.TestCase):
    def test_django_identifiers_remain_quoted(self):
        self.assertEqual(oracle_identifier('ACCESS_USER'), '"ACCESS_USER"')
        self.assertEqual(oracle_identifier('table_123'), '"table_123"')

    def test_sql_punctuation_and_empty_names_are_refused(self):
        for name in ('', 'a" WHERE 1=1 --', 'a.b', 'a;DROP TABLE USERS', 'a\x00b', 'a b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                oracle_identifier(name)
