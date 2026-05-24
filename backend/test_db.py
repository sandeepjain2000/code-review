import asyncio
from services.jobs_service import update_job, create_job

async def main():
    await create_job('test2', 'path')
    await update_job('test2', total_files=50)
    print('DONE')

asyncio.run(main())
