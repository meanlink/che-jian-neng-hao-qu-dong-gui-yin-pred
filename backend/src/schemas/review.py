from pydantic import BaseModel, Field


class PlanInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    destination: str = Field(default='业务复核组 · 本地收件箱', min_length=2, max_length=120)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    run_time: str = Field(default='08:00', max_length=5)
    start_date: str
    holidays: list[str] = Field(default_factory=list)


class PlanUpdate(BaseModel):
    destination: str | None = Field(default=None, min_length=2, max_length=120)
    weekdays: list[int] | None = None
    run_time: str | None = Field(default=None, max_length=5)
    holidays: list[str] | None = None


class PlanToggle(BaseModel):
    enabled: bool


class LineInput(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    dataset_id: int | None = None
    params: dict = Field(default_factory=dict)
    enabled: bool = True


class BranchDataInput(BaseModel):
    dataset_id: int
