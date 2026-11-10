import hashlib
import unittest

from app import password_hash, password_hash_needs_upgrade, verify_password


class PasswordSecurityTests(unittest.TestCase):
    def test_new_hash_is_salted_and_verifies(self):
        first = password_hash("Strong-Password-1", salt=b"\x01" * 16, iterations=100000)
        second = password_hash("Strong-Password-1", salt=b"\x02" * 16, iterations=100000)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("Strong-Password-1", first))
        self.assertFalse(verify_password("wrong", first))
        self.assertFalse(password_hash_needs_upgrade(first))

    def test_legacy_sha256_is_verified_and_marked_for_upgrade(self):
        legacy = hashlib.sha256("admin123".encode("utf-8")).hexdigest()
        self.assertTrue(verify_password("admin123", legacy))
        self.assertFalse(verify_password("wrong", legacy))
        self.assertTrue(password_hash_needs_upgrade(legacy))

    def test_malformed_hash_fails_closed(self):
        self.assertFalse(verify_password("password", "pbkdf2_sha256$bad"))
        self.assertFalse(verify_password("password", ""))


if __name__ == "__main__":
    unittest.main()
