# Project instructions

## Local Docker lifecycle

After each local Docker test or repair experiment, stop Colima with `colima stop`
and verify it has stopped, including when tests fail or are interrupted. Start
it again only when needed for the next Docker run. Removing test containers alone
is not sufficient; do not leave the Docker VM running after testing.
