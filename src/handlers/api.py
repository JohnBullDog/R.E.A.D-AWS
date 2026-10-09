"""API Lambda: the R.E.A.D. pages and JSON API behind API Gateway (HTTP API), via Mangum."""

from mangum import Mangum

from read.jobs import Env
from read.webapp import create_app

handler = Mangum(create_app(Env.from_environ()), lifespan="off")
