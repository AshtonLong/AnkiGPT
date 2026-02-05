from typing import List, Literal, Optional
from pydantic import BaseModel, field_validator, model_validator


class CardSchema(BaseModel):
    type: Literal["basic", "cloze"]
    front: Optional[str] = None
    back: Optional[str] = None
    cloze_text: Optional[str] = None
    extra: Optional[str] = ""
    tags: List[str] = []

    @model_validator(mode="after")
    def validate_fields(self):
        if self.type == "basic":
            if not self.front or not self.back:
                raise ValueError("basic cards require front and back")
        if self.type == "cloze":
            if not self.cloze_text:
                raise ValueError("cloze cards require cloze_text")
        return self

    @field_validator("front", "back", "cloze_text", "extra")
    @classmethod
    def strip_fields(cls, value):
        if value is None:
            return value
        return value.strip()


class ChunkSchema(BaseModel):
    cards: List[CardSchema]
