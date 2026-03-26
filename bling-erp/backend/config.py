from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    CLIENT_ID: str = Field(
        default="86f6eb0eb0df9c8bb9dc6751b3518cc6a7486f2d",
        description="Bling OAuth client_id (username do Basic Auth)",
    )
    CLIENT_SECRET: str = Field(
        default="83f4a138225543bb8cdfb49dca7b628da707f91179a718aca78e57e415da",
        description="Bling OAuth client_secret (password do Basic Auth)",
    )
    INITIAL_ACCESS_TOKEN: str = Field(
        default=(
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJpZCI6IjNlMWYyOGVhYmQ0ODg3YmY5OWQyZTZjMTI0ZWE0ODdhODJmM2FiMGEiLCJqdGkiOiIzZTFmMjhlYWJkNDg4N2JmOTlkMmU2YzEyNGVhNDg3YTgyZjNhYjBhIiwiaXNzIjpudWxsLCJhdWQiOiI4NmY2ZWIwZWIwZGY5YzhiYjlkYzY3NTFiMzUxOGNjNmE3NDg2ZjJkIiwic3ViIjoiMTQ3ODQyNjgzMzgiLCJleHAiOjE3NzQzOTU5NTYsImlhdCI6MTc3NDM3NDM1NiwidG9rZW5fdHlwZSI6ImJlYXJlciIsInNjb3BlIjoiVGRLTGpjUXdDQVRRVmlnQkJ2UHJ2N0diOVc3SVNSRjVVZ3pZSnRPdUxjTTRuMmg2bzk5NGJnd3hQWFp3WCtsOEZTRStYbEFKclRrdVVURjZ4Q0k5REJJekdwRmNtNVpkckdvTjRPaVUyQXdLalJHb3RsSW1nQ1lzNjRoeklZdWRmaFM2OHRXdWkxamxvOFJxTTNJemNqTnllOVQyZUhkUVc2OXF0Um05R1cycjdkYnM0YnlncE1YVEJ4WTlqMFpYdHNMS1YyZlZxMitWY0Y3V1RcL2ZrVjZWVXFcL29jNXRhd2FSOU9wU3VNWStCT0hcL25xXC9jbzU1eGtNejhaQkZpZFRLRE5PTGo5ZGVDNkpUc0RhZVBvRXU1ZzU2a3Z3cnhneHp4TnFHTjdlK2pQemRUOTJQcThSXC81eXZZXC80QSIsImdyYW50VHlwZXMiOiJhdXRob3JpemF0aW9uX2NvZGUgcmVmcmVzaF90b2tlbiIsImFwcF9pZCI6IjMyOTA3NiIsImNvbXBhbnlfaWQiOjE0OTAyMTcyODc1LCJyb2xlIjoiYWRtIiwicGxhbl9uYW1lIjoiUGxhbm9Db2JhbHRvIiwiYXBwcm92ZWQiOmZhbHNlfQ.NR8Zz2H6B4OHSmEzMLChO-HpH6XT9iBbqce-Ci05clfpjVf3nZ2_jotEvzLMdPfOWPUU-a6NYXg1ZdEfUrPzxIDmEcKvVkRBfwJXdB0ERPfUpJK1ivfl034DZsC8tKDuvNETKsL3VDnLMjFz2QqZBp6uP4eC43YLuzDc8gdN1qSH2GrfY3Boii-0tRIxpuO1V4JDG051D60wEhmUiv432rfifbSTiBYoxFpquafLVkXCyGlCBW_ElprftJ1469bMJYmSnl7uQphTMjbaU942HYps4h-9Jjq-xhKh1HhcsnCmG2jFpVGLw3KmTW9XDiQIO7Redfs2ZOnV8fpSIjjJyQ"
        ),
        description="Access token inicial obtido manualmente",
    )
    INITIAL_REFRESH_TOKEN: str = Field(
        default="6cf3a1baac4365678d65a0ed419517b023abfca9",
        description="Refresh token para renovar o access token",
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
