# Optional production reference: scheduled HPA pre-scaling

This example demonstrates scheduled pre-scaling for predictable traffic peaks.
It is not part of the Golden Path assessment and is not applied by `make deploy`.

The assessment uses the Kubernetes Horizontal Pod Autoscaler (HPA).
Production pre-scaling should only be enabled after validating historical demand,
per-pod capacity, startup time, cluster resources, database limits and timezone.

The illustrative schedule raises the HPA minimum to 5 on Thursdays at 08:45
and restores it to 3 at 10:00, using `Europe/London`. These are not production
sizing recommendations.

## Apply

The application and HPA must already exist in the `bookings` namespace.

Create the ConfigMap:

    kubectl -n bookings create configmap availability-api-prescale-script \
      --from-file=prescale_hpa.py=examples/production/prescaling/prescale_hpa.py \
      --dry-run=client -o yaml | kubectl apply -f -

Apply the optional resources:

    kubectl apply -f examples/production/prescaling/prescale.yaml

Verify:

    kubectl -n bookings get cronjobs
    kubectl -n bookings get serviceaccount,role,rolebinding | grep prescaler
    kubectl -n bookings get hpa availability-api

## Remove

    kubectl -n bookings delete -f examples/production/prescaling/prescale.yaml
    kubectl -n bookings delete configmap availability-api-prescale-script --ignore-not-found

Removing these resources does not remove or modify the application HPA.

## Operational considerations

Scheduled pre-scaling is not a capacity guarantee. It can reserve resources
even when the expected traffic does not arrive. Consider event-driven or
predictive scaling when reliable leading indicators exist. Measure the
reliability benefit and idle-capacity cost before retaining a production schedule.
