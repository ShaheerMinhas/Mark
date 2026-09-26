from backend.services.EmailModule.providers.base import ProviderFind, ProviderVerify


class FakeProvider:
    def __init__(self, name: str, email=None, verify="unknown", available=True, http_find=200, http_verify=200):
        self.name = name
        self._email = email
        self._verify = verify
        self._available = available
        self._http_find = http_find
        self._http_verify = http_verify
        self.find_calls = 0
        self.verify_calls = 0

    def available(self) -> bool:
        return self._available

    def find_email(self, **kwargs):
        self.find_calls += 1
        if self._http_find == 429:
            return ProviderFind(http_status=429, detail="rate_limited")
        return ProviderFind(email=self._email, http_status=self._http_find, credits=1)

    def verify_email(self, email: str):
        self.verify_calls += 1
        if self._http_verify == 429:
            return ProviderVerify(status="blocked", http_status=429, detail="rate_limited")
        return ProviderVerify(status=self._verify, http_status=self._http_verify, credits=1)
