class GovError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}
