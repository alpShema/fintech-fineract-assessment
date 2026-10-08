from pydantic import BaseModel, ConfigDict


class ClientCreate(BaseModel):
    external_reference: str
    client_uid: str
    firstname: str
    lastname: str


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    client_uid: str
    status: str
    display_name: str
