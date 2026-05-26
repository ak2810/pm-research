from pydantic import BaseModel, ConfigDict


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    t_recv_ns: int
    feed: str
