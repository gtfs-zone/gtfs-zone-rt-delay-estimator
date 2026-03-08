from sqlmodel import Field, SQLModel


class GtfsStaticFeed(SQLModel, table=True):
    __tablename__ = "gtfs_static_feed"

    id: int | None = Field(default=None, primary_key=True)
    timezone: str | None = None


class Feed(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    gtfs_static_feed_id: int | None = None


class GtfsStop(SQLModel, table=True):
    __tablename__ = "gtfs_stop"

    id: int | None = Field(default=None, primary_key=True)
    gtfs_static_feed_id: int
    stop_id: str
    stop_lat: float
    stop_lon: float


class GtfsStopTime(SQLModel, table=True):
    __tablename__ = "gtfs_stop_time"

    id: int | None = Field(default=None, primary_key=True)
    gtfs_static_feed_id: int
    trip_id: str
    stop_id: str
    arrival_time: str
    departure_time: str
    stop_sequence: int
