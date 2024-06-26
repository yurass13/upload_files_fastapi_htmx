import csv
import os
from typing import AsyncGenerator

import aiofiles
from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select, update

from .db import ProcessingFile, ProcessingFileStatus, session_factory
from .services import emit_file_status_changed, subscribe_file_status_changed
from .templates import templates
from .worker import process_file, terminate_task

CHUNK_SIZE = 1024 * 1024

processing_file_router = APIRouter()


@processing_file_router.post("/")
async def create_processing_file(request: Request, file: UploadFile):
    if file.content_type != "text/csv":
        return templates.TemplateResponse(request=request,
                                          name="primitives/upload/dismiss.html",
                                          context={"filename": file.filename})

    processing_file = ProcessingFile(filename=file.filename)
    file_id = None
    async with session_factory() as session:
        session.add(processing_file)

        await session.flush()
        file_id = processing_file.id

        try:
            async with aiofiles.open(os.path.abspath(f"./media/processing_files/{file_id}"), 'wb') as writer:
                while chunk := await file.read(CHUNK_SIZE):
                    await writer.write(chunk)

        except Exception:
            processing_file.status = ProcessingFileStatus.ERROR
            return templates.TemplateResponse(request=request,
                                          name="primitives/upload/dismiss.html",
                                          context={"filename": file.filename})
        finally:
            await file.close()
            await session.commit()

    task = process_file.delay(file_id)

    async with session_factory() as session:
        await session.execute(update(ProcessingFile)
                                .values(task_id=task.id)
                                    .filter_by(id=file_id))
        await session.commit()

    return templates.TemplateResponse(request=request,
                                      name="primitives/upload/success.html",
                                      context={"filename": file.filename})


@processing_file_router.get("/{file_id}/detail/")
async def get_processing_file_detail(request: Request, file_id: int):
    # TODO optimize for large files
    async with session_factory() as session:
        result = await session.execute(select(ProcessingFile).filter_by(id=file_id))
        processing_file = result.scalar()

        try:
            # TODO move to get_data_sample - separate endpoint. Add sample size option. Add pagination. 
            with open(processing_file.file_path, 'r') as file:
                rows = []
                for row in csv.reader(file):
                    if len(rows) >= 10:
                        break

                    rows.append(row)

        except FileNotFoundError:
            rows = None

        return templates.TemplateResponse(request=request,
                                        name="processing_files/detail.html",
                                        context={"file": processing_file,
                                                "data": rows})


@processing_file_router.delete("/{file_id}/")
async def cancel_or_delete_processing_file(file_id: int):
    async with session_factory() as session:
        result = await session.execute(select(ProcessingFile).filter_by(id=file_id))
        processing_file = result.scalar()

        if processing_file is None:
            raise ValueError()

        if processing_file.task_id:
            terminate_task(processing_file.task_id)

        if processing_file.status == ProcessingFileStatus.OK:
            status = ProcessingFileStatus.REMOVED
            if os.path.exists(processing_file.file_path):
                os.remove(processing_file.file_path)

        else:
            status = ProcessingFileStatus.CANCELED
        stmt = (
            update(ProcessingFile)
                .values(status=status, task_id=None)
                    .filter_by(id=file_id))

        await session.execute(stmt)
        await session.commit()

    await emit_file_status_changed(file_id=file_id)


@processing_file_router.get("/listen-updates/")
async def listen_handling_status(request: Request):
    return StreamingResponse(get_files_on_status_changed(request), media_type="text/event-stream")


async def get_files_on_status_changed(request: Request) -> AsyncGenerator[str, None]:
    while True:
        async with session_factory() as session:
            result = await session.execute(select(ProcessingFile).order_by(desc(ProcessingFile.created_at)))
            files = result.scalars()
            template = templates.TemplateResponse("processing_files/list.html", 
                                                  context={"request": request,
                                                           "files": files}
                                                  )
        content = template.body.decode("utf-8").replace("\n", "")

        yield f"event: handlingStatusChanged\ndata: {content}\n\n"

        await subscribe_file_status_changed()
