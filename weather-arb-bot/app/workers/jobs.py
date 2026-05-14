async def job_fetch_models():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        dates = [today + timedelta(days=i) for i in range(FORECAST_DAYS_AHEAD)]
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            lat, lon = float(city.nws_lat), float(city.nws_lon)
            for d in dates:
                for model in ("gfs", "ecmwf"):
                    try:
                        await gfs_col.collect_and_store(city.id, lat, lon, d, db, model)
                    except Exception as e:
                        logger.error(f"{model} job failed for {city.name} {d}: {e}")
                # GFS ensemble probabilities (once per date, not per model)
                try:
                    await gfs_col.collect_ensemble_and_store(city.id, lat, lon, d, db)
                except Exception as e:
                    logger.error(f"GFS ensemble job failed for {city.name} {d}: {e}")