class MemfwError(Exception):
    """Base error for user-correctable vault operations."""


class IntegrityError(MemfwError):
    pass


class NotFoundError(MemfwError):
    pass


class DeletedContentUnavailable(MemfwError):
    pass
