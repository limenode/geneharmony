from datetime import datetime
from pydantic import BaseModel, PositiveInt

class DownloadFile(BaseModel):
    filename: str
    s3Path: str
    s3Url: str
    stableURL: str
    releaseVersion: str
    size: PositiveInt
    lastModified: datetime
    dataType: str
    fileType: str
    dataSubType: str
    fileExtension: str